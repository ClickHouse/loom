"""Sustained write load, its latency, and the ClickHouse parts it made.

Writes at a fixed rate for a fixed time, open loop: write i starts at
start + i / rate whether or not earlier ones have finished. Prints the achieved
rate, p50/p95/p99 latency, errors, and from `system.part_log` the new parts each
table of the schema got while it ran, and from `system.query_log` how many
conversation inserts carried each `async_insert` value, so the output shows which
form ran. Free with the fake embedder and LLM.

`--target server` sends `memory.set` and/or `conversation.append` to a running
server (`--op`). `--target client` runs, in this process, the two inserts a
`conversation.append` makes (`loom.conversations`, then
`loom.conversation_embeddings`) with the settings the store sends (the turn
batched with `batched_insert_settings()`, the embedding row with
`embedding_insert_settings`), or with `--committed` as two committed inserts:
the insert path alone, at rates one server process cannot serve. Both read the
ClickHouse settings (`LOOM_CH_*`, `.env`) to query the part log.

A token (`--token`) covers one org, so `--orgs` above 1 needs a server with
`LOOM_AUTH_REQUIRED=false`; spreading over orgs keeps the org-wide rate limit
from capping the rate.

The rows land in the ClickHouse the settings name, under the org `--org`
(default `write-rate-<run>`, one per run). `--target client` deletes the
conversation rows the run wrote when it ends. `--target server` keeps what it wrote (the
memories, the transcript turns, conversation logging switched on for each org and
namespace it wrote to), so point it at a private ClickHouse.

    PYTHONPATH=$PWD .venv/bin/python bench/write_rate.py --target client --rate 200 --seconds 60
    PYTHONPATH=$PWD .venv/bin/python bench/write_rate.py --target client --rate 200 --seconds 60 \
        --committed
    PYTHONPATH=$PWD .venv/bin/python bench/write_rate.py --target server \
        --base-url http://localhost:7790 --op append --rate 100 --seconds 60 \
        --namespaces 128 --orgs 128
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from server.core.ch import get_client
from server.core.ch_writes import batched_insert_settings
from server.knowledge.store.conversations import embedding_insert_settings

_COMMITTED = {"async_insert": 0, "wait_for_async_insert": 1}
_SERVER_OPS = {
    "set": ["memory.set"],
    "append": ["conversation.append"],
    "mix": ["memory.set", "conversation.append"],
}


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


def _namespace(i: int, args: argparse.Namespace) -> str:
    # A chat product writes for many users, and each namespace has its own
    # rate-limit bucket.
    return f"{args.namespace}-{i % args.namespaces}"


def _orgs(args: argparse.Namespace) -> list[str]:
    return [_org(i, args) for i in range(args.orgs)]


async def _delete_client_rows(run: str, args: argparse.Namespace) -> None:
    # Only this run's rows: an existing org given with --org keeps its own turns.
    ch = get_client()
    for table in ("conversations", "conversation_embeddings"):
        await ch.execute(
            f"DELETE FROM loom.{table} WHERE org IN {{orgs:Array(String)}}"
            " AND startsWith(thread_id, {prefix:String})",
            params={"orgs": _orgs(args), "prefix": f"{run}-thread-"},
        )


def _org(i: int, args: argparse.Namespace) -> str:
    return args.org if args.orgs == 1 else f"{args.org}-{i % args.orgs}"


def _server_body(op: str, i: int, run: str, args: argparse.Namespace) -> dict[str, Any]:
    base = {"org": _org(i, args), "namespace": _namespace(i, args), "agent": f"agent-{i % 8}"}
    if op == "memory.set":
        return {
            **base,
            "memory_key": f"load/{run}/fact-{i}",
            "value": f"Customer {i} of run {run} prefers invoices in format {i % 13}.",
        }
    return {
        **base,
        "thread_id": f"{run}-thread-{i % 50}",
        "msg_id": f"{run}-{i}",
        "role": "user" if i % 2 else "assistant",
        "content": f"Turn {i} of run {run}: the delivery for order {i} moved to day {i % 28}.",
    }


async def _client_write(i: int, run: str, args: argparse.Namespace) -> None:
    ch = get_client()
    settings = dict(_COMMITTED) if args.committed else batched_insert_settings()
    ts = datetime.now(UTC).replace(tzinfo=None)
    turn = {
        "org": _org(i, args),
        "namespace": _namespace(i, args),
        "agent": f"agent-{i % 8}",
        "thread_id": f"{run}-thread-{i % 50}",
        "msg_id": f"{run}-{i}",
        "role": "user",
        "user_id": "-",
        "run_id": "-",
        "is_saved": False,
        "saved_at": None,
        "ts": ts,
    }
    content = f"Turn {i} of run {run}: the delivery for order {i} moved to day {i % 28}."
    await ch.insert(
        "loom.conversations",
        [{**turn, "content": content, "reasoning": "", "metadata": "{}", "tokens": 0}],
        settings=settings,
    )
    await ch.insert(
        "loom.conversation_embeddings",
        [
            {
                **turn,
                "content_excerpt": content,
                # Stored as a failed embed (no vector), so the run needs no embedder
                # and fits any column width.
                "embedding_model": "write-rate",
                "embedding_dim": 0,
                "embedding": [],
                "embed_attempts": 0,
                "embed_last_error": "write-rate: not embedded",
                "embed_failed_at": ts,
            }
        ],
        settings=dict(_COMMITTED) if args.committed else embedding_insert_settings([]),
    )


async def _parts(since: float, until: float) -> dict[str, dict[str, int]]:
    ch = get_client()
    await ch.execute("SYSTEM FLUSH LOGS")
    rows = await ch.query_all(
        "SELECT table, count() AS parts, sum(rows) AS rows FROM system.part_log "
        "WHERE event_type = 'NewPart' AND database = currentDatabase() "
        "AND event_time BETWEEN toDateTime({since:UInt32}) AND toDateTime({until:UInt32}) "
        "GROUP BY table ORDER BY parts DESC",
        {"since": int(since), "until": int(until) + 2},
    )
    return {str(r["table"]): {"parts": int(r["parts"]), "rows": int(r["rows"])} for r in rows}


async def _inserts_sent(since: float, until: float) -> dict[str, dict[str, int]]:
    """Per table, how many inserts the query log saw with each `async_insert` value:
    the proof of which form the run actually sent."""
    ch = get_client()
    rows = await ch.query_all(
        "SELECT splitByChar('.', tables[1])[2] AS table, "
        "Settings['async_insert'] AS async_insert, count() AS n "
        "FROM system.query_log "
        "WHERE type = 'QueryFinish' AND query_kind = 'Insert' AND length(tables) = 1 "
        "AND tables[1] IN (currentDatabase() || '.conversations', "
        "currentDatabase() || '.conversation_embeddings') "
        "AND event_time BETWEEN toDateTime({since:UInt32}) AND toDateTime({until:UInt32}) "
        "GROUP BY table, async_insert ORDER BY table, async_insert",
        {"since": int(since), "until": int(until) + 2},
    )
    # The log leaves out a setting sent at the server's default value, which is
    # 1 on recent ClickHouse releases, so an unset value is that default.
    row = await ch.query_one("SELECT default FROM system.settings WHERE name = 'async_insert'")
    default = row["default"] if row else "0"
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        value = r["async_insert"] or default
        out.setdefault(str(r["table"]), {})[f"async_insert={value}"] = int(r["n"])
    return out


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    await get_client().connect()
    run = uuid.uuid4().hex[:8]
    if not args.org:
        args.org = f"write-rate-{run}"
    kinds = _SERVER_OPS[args.op] if args.target == "server" else ["conversation.append inserts"]
    latencies: dict[str, list[float]] = {k: [] for k in kinds}
    errors: dict[str, int] = {k: 0 for k in kinds}
    first_error: dict[str, str] = {}
    limits = httpx.Limits(max_connections=args.max_in_flight)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=120, limits=limits, headers=headers
    ) as http:
        if args.target == "server" and "conversation.append" in kinds:
            # Every (org, namespace) pair a write can name.
            for n in range(math.lcm(args.orgs, args.namespaces)):
                r = await http.post(
                    "/v1/conversation.set_logging",
                    json={"org": _org(n, args), "namespace": _namespace(n, args), "enabled": True},
                )
                r.raise_for_status()

        async def server_write(i: int, kind: str) -> None:
            r = await http.post(f"/v1/{kind}", json=_server_body(kind, i, run, args))
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} {r.text[:300]}")
            if r.json().get("stored") is False:
                raise RuntimeError(f"not stored: {r.text[:300]}")

        async def one(i: int, at: float) -> None:
            kind = kinds[i % len(kinds)]
            await asyncio.sleep(max(0.0, at - time.monotonic()))
            sent = time.monotonic()
            try:
                if args.target == "server":
                    await server_write(i, kind)
                else:
                    await _client_write(i, run, args)
                latencies[kind].append((time.monotonic() - sent) * 1000)
            except Exception as e:  # noqa: BLE001 — counted and reported, the run goes on
                errors[kind] += 1
                first_error.setdefault(kind, str(e)[:300])

        started = time.time()
        start = time.monotonic() + 0.5
        total = int(args.rate * args.seconds)
        try:
            await asyncio.gather(*(one(i, start + i / args.rate) for i in range(total)))
            elapsed = time.monotonic() - start
            parts = await _parts(started, time.time())
            sent = await _inserts_sent(started, time.time())
        finally:
            if args.target == "client":
                await _delete_client_rows(run, args)

    return {
        "target": args.target,
        "org": args.org if args.orgs == 1 else f"{args.org}-*",
        "op": args.op if args.target == "server" else "conversation.append inserts",
        "inserts": "committed" if args.committed else "as the code sends them",
        "inserts_sent": sent,
        "target_rate": args.rate,
        "seconds": args.seconds,
        "achieved_rate": round(sum(len(v) for v in latencies.values()) / elapsed, 1),
        "latency_ms": {
            k: {
                "n": len(v),
                "p50": _percentile(v, 0.50),
                "p95": _percentile(v, 0.95),
                "p99": _percentile(v, 0.99),
            }
            for k, v in latencies.items()
        },
        "errors": errors,
        "first_error": first_error,
        "new_parts_total": sum(p["parts"] for p in parts.values()),
        "new_parts_by_table": parts,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--target", choices=("server", "client"), default="client")
    p.add_argument("--base-url", default="http://localhost:7790")
    p.add_argument("--op", choices=tuple(_SERVER_OPS), default="mix", help="--target server only")
    p.add_argument("--committed", action="store_true", help="--target client only")
    p.add_argument("--rate", type=float, default=200.0, help="writes per second")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--max-in-flight", type=int, default=512)
    p.add_argument("--token", default="", help="bearer token for --target server")
    p.add_argument(
        "--org", default="", help="org, or org prefix with --orgs (default write-rate-<run>)"
    )
    p.add_argument("--orgs", type=int, default=1, help="orgs the writes spread over")
    p.add_argument("--namespace", default="write-rate", help="namespace prefix")
    p.add_argument("--namespaces", type=int, default=32, help="namespaces the writes spread over")
    p.add_argument("--out", default="", help="also write the JSON result here")
    args = p.parse_args()
    if args.committed and args.target == "server":
        p.error("--committed applies to --target client: a server sends what its code sends")
    text = json.dumps(asyncio.run(_run(args)), indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
