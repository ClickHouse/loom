"""Nested key-value retrieval against loom — the multi-hop lookup cliff.

The task stores 140 UUID→UUID pairs (~8k tokens), asks for the value of one
key, and makes the value itself a key for `level` more hops. A fixed-context
reader collapses within a few levels; an agent that can issue one lookup per
hop is flat across levels. It is the cleanest measurement of "one retrieval
pass has a depth cliff, chaining removes it".

Loom's answer to multi-hop is the typed-triple walk
(`facts.traverse`) when triples exist, and `memory.ask`'s depth-one expansion
when they do not. This harness measures where loom's own cliff is on
identifiers that carry no semantics, with four arms:

  ask          memory.ask with the nested-lookup instruction; the server's
               single pass (decomposition, expansion, deep pass as configured).
  search_k     one memory.search(query=key, top_k=K): does the final value
               appear anywhere in the K rows a fixed-context reader would see?
  chain        the agent loop, deterministic: memory.search(query=
               current key, top_k=3) per hop, follow "Key: X, Value: Y" until
               Y is not a key. One lookup per hop, no model.
  recall_chain the same loop over memory.recall — the raw
               turns by exact token — so the lookup is lexical, not a vector.
               Needs `--write-mode extract`: recall reads the turns behind
               `set_from_messages`; a `memory.set` corpus leaves none.

Per (level, ordering) one throwaway namespace holds the pairs. The default
write is `memory.set` (key `kv_<uuid>`, value "Key: <k>, Value: <v>") so
nothing is extracted or dropped and the arms measure retrieval alone;
`--write-mode extract` sends the pairs as one `set_from_messages` call
instead, which also exercises the extractor on identifiers. The question
names the first key. Accuracy is exact containment of the final value.
Reported per level per arm, with lookups per solved question.

    LOOM_API_KEY=... python bench/nested_kv.py --base-url http://localhost:7777 \\
        --pairs 140 --levels 0,1,2,3,4 --orderings 3 --arms ask,search_k,chain

Cost: pairs × orderings × levels writes (2,100 at the defaults) and one
memory.ask per question on the `ask` arm. `--orderings 30` is the published task size.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
from typing import Any

try:
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# ---------------------------------------------------------------------------
# Pure task construction and scoring
# ---------------------------------------------------------------------------


def build_task(*, pairs: int, level: int, seed: int) -> dict[str, Any]:
    """`pairs` UUID→UUID rows, `level` of them chained from the start key.

    level 0: the start key's value is not a key. level n: start → v1 → … →
    v_n, and v_n is not a key. Chain rows sit at seeded random positions in
    the write order, varying both the initial key position and the nesting
    key positions."""
    rng = random.Random(seed)
    ids = [
        str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(pairs * 2 + level + 2)
    ]
    it = iter(ids)
    chain_keys = [next(it) for _ in range(level + 1)]
    final_value = next(it)
    rows: list[tuple[str, str]] = []
    for i, k in enumerate(chain_keys):
        v = chain_keys[i + 1] if i + 1 < len(chain_keys) else final_value
        rows.append((k, v))
    while len(rows) < pairs:
        rows.append((next(it), next(it)))
    rng.shuffle(rows)
    return {
        "level": level,
        "seed": seed,
        "start_key": chain_keys[0],
        "chain": chain_keys,
        "final_value": final_value,
        "rows": rows,
    }


def value_of(text: str, key: str) -> str | None:
    """The value paired with `key` in a "Key: k, Value: v" row, if present."""
    m = re.search(rf"Key:\s*{re.escape(key)}\s*,\s*Value:\s*({_UUID_RE.pattern})", text, re.I)
    return m.group(1).lower() if m else None


def contains_final(texts: list[str], final_value: str) -> bool:
    fv = final_value.lower()
    return any(fv in (t or "").lower() for t in texts)


def follow_chain(lookup: Any, start_key: str, *, max_hops: int = 8) -> tuple[str | None, int]:
    """Synchronous driver used by tests: `lookup(key) -> list[str]` returns the
    texts a lookup surfaced. Returns (final value or None, lookups made) —
    two lookups per hop: one to read the value, one to learn it is not a key."""
    key = start_key
    lookups = 0
    for _ in range(max_hops):
        texts = lookup(key)
        lookups += 1
        nxt = next((v for t in texts if (v := value_of(t, key))), None)
        if nxt is None:
            return None, lookups
        lookups += 1
        if not any(value_of(t, nxt) for t in lookup(nxt)):
            return nxt, lookups
        key = nxt
    return None, lookups


def instruction(start_key: str) -> str:
    return (
        f"Find the value for key {start_key}. Every memory is one 'Key: <uuid>, "
        "Value: <uuid>' pair. If the value you find is itself a key of another "
        "pair, look that key up too, and keep going until the value is not a key. "
        "Answer with only the final value."
    )


# ---------------------------------------------------------------------------
# Wire
# ---------------------------------------------------------------------------


# Retry posture: a 429 is the per-token limiter and honours Retry-After; a 5xx
# is the server or its store — a table restart (TABLE_IS_BEING_RESTARTED after a Keeper
# session expiry) can outlast a 30-second backoff. Eight attempts with a 45-second cap
# wait about three minutes in total, which a rolling table restart fits inside; a longer
# outage still fails the task, and the run reports it rather than hanging.
MAX_ATTEMPTS = 8
BACKOFF_CAP_S = 45.0


def backoff_seconds(attempt: int, status: int, retry_after: float = 0.0) -> float:
    """Seconds to wait after `attempt` (0-based) failed with `status`."""
    wait = min(1.0 * (2**attempt), BACKOFF_CAP_S)
    return max(wait, retry_after) if status == 429 else wait


async def _post(client: Any, url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
    import httpx

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last: Any = None
    for attempt in range(MAX_ATTEMPTS):
        r = await client.post(url, json=body, headers=headers, timeout=180.0)
        if r.status_code < 500 and r.status_code != 429:
            r.raise_for_status()
            return r.json()
        last = r
        try:
            retry_after = float(r.headers.get("retry-after") or 0)
        except ValueError:
            retry_after = 0.0
        await asyncio.sleep(backoff_seconds(attempt, r.status_code, retry_after))
    if last is not None:
        last.raise_for_status()
    raise httpx.HTTPError("unreachable")


async def write_task(
    client: Any,
    task: dict[str, Any],
    *,
    base_url: str,
    token: str,
    identity: dict[str, Any],
    write_mode: str = "set",
) -> int:
    sem = asyncio.Semaphore(4)
    t0 = time.perf_counter()
    if write_mode == "extract":
        await _post(
            client,
            base_url + "/v1/memory.set_from_messages",
            {
                **identity,
                "session_id": f"{identity['namespace']}:pairs",
                "messages": [
                    {"role": "user", "content": f"Key: {k}, Value: {v}"} for k, v in task["rows"]
                ],
            },
            token,
        )
        return int((time.perf_counter() - t0) * 1000)

    async def _one(k: str, v: str) -> None:
        async with sem:
            await _post(
                client,
                base_url + "/v1/memory.set",
                {
                    **identity,
                    "memory_key": f"kv_{k}",
                    "value": f"Key: {k}, Value: {v}",
                    "memory_type": "semantic",
                    "tags": ["nested_kv"],
                },
                token,
            )

    await asyncio.gather(*[_one(k, v) for k, v in task["rows"]])
    return int((time.perf_counter() - t0) * 1000)


async def run_task(
    client: Any,
    task: dict[str, Any],
    *,
    base_url: str,
    token: str,
    org: str,
    arms: list[str],
    top_k: int,
    ask_json: dict[str, Any],
    search_json: dict[str, Any],
    write_mode: str = "set",
    keep: bool = False,
) -> dict[str, Any]:
    ns = f"bench-nestedkv-{uuid.uuid4().hex[:10]}"
    remember_bench_namespace(org, ns)
    await register_bench_namespace(
        lambda p, b: _post(client, base_url + p, b, token),
        org=org,
        namespace=ns,
        description=f"nested KV level {task['level']} seed {task['seed']}",
        keep=keep,
    )
    identity = {"org": org, "namespace": ns, "agent": "nested-kv-bench", "user_id": "-"}
    write_ms = await write_task(
        client, task, base_url=base_url, token=token, identity=identity, write_mode=write_mode
    )
    row: dict[str, Any] = {
        "level": task["level"],
        "seed": task["seed"],
        "namespace": ns,
        "write_ms": write_ms,
    }
    fv = task["final_value"]

    async def _search_texts(key: str, k: int) -> list[str]:
        resp = await _post(
            client,
            base_url + "/v1/memory.search",
            {
                **identity,
                "query": key,
                "top_k": k,
                "bodies": "full",
                "namespace_scope": "this",
                **search_json,
            },
            token,
        )
        return [str(h.get("content_excerpt") or "") for h in resp.get("results", [])]

    async def _recall_texts(key: str, k: int) -> list[str]:
        resp = await _post(
            client,
            base_url + "/v1/memory.recall",
            {**identity, "query": key, "match": "all", "top_k": k, "max_chars": 400},
            token,
        )
        return [str(t.get("content") or "") for t in resp.get("turns", [])]

    async def _chain(fetch: Any, k: int) -> tuple[bool, int, int]:
        key = task["start_key"]
        lookups = 0
        t0 = time.perf_counter()
        for _ in range(8):
            texts = await fetch(key, k)
            lookups += 1
            nxt = next((v for t in texts if (v := value_of(t, key))), None)
            if nxt is None:
                return False, lookups, int((time.perf_counter() - t0) * 1000)
            texts2 = await fetch(nxt, k)
            lookups += 1
            if not any(value_of(t, nxt) for t in texts2):
                return nxt == fv.lower(), lookups, int((time.perf_counter() - t0) * 1000)
            key = nxt
        return False, lookups, int((time.perf_counter() - t0) * 1000)

    if "ask" in arms:
        t0 = time.perf_counter()
        resp = await _post(
            client,
            base_url + "/v1/memory.ask",
            {
                **identity,
                "query": instruction(task["start_key"]),
                "top_k": top_k,
                "namespace_scope": "this",
                **ask_json,
            },
            token,
        )
        answer = str(resp.get("answer") or "")
        row["ask_correct"] = int(contains_final([answer], fv))
        row["ask_abstain"] = int(bool(resp.get("abstain")))
        row["ask_ms"] = int((time.perf_counter() - t0) * 1000)
        row["ask_llm_calls"] = resp.get("llm_calls")
    if "search_k" in arms:
        t0 = time.perf_counter()
        texts = await _search_texts(task["start_key"], top_k)
        row["search_k_correct"] = int(contains_final(texts, fv))
        row["search_k_ms"] = int((time.perf_counter() - t0) * 1000)
    if "chain" in arms:
        ok, n, ms = await _chain(_search_texts, 3)
        row.update({"chain_correct": int(ok), "chain_lookups": n, "chain_ms": ms})
    if "recall_chain" in arms:
        try:
            ok, n, ms = await _chain(_recall_texts, 3)
            row.update(
                {
                    "recall_chain_correct": int(ok),
                    "recall_chain_lookups": n,
                    "recall_chain_ms": ms,
                }
            )
        except Exception as e:  # noqa: BLE001 — the op may be absent on an older server
            row["recall_chain_error"] = str(e)[:120]
    return row


def summarize(rows: list[dict[str, Any]], arms: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(rows), "by_level": {}}
    levels = sorted({int(r["level"]) for r in rows})
    for lv in levels:
        rs = [r for r in rows if int(r["level"]) == lv]
        cell: dict[str, Any] = {"n": len(rs)}
        for arm in arms:
            key = f"{arm}_correct"
            vals = [r[key] for r in rs if key in r]
            if vals:
                cell[arm] = round(sum(vals) / len(vals), 3)
            lk = [r[f"{arm}_lookups"] for r in rs if f"{arm}_lookups" in r]
            if lk:
                cell[f"{arm}_lookups_mean"] = round(sum(lk) / len(lk), 2)
        out["by_level"][str(lv)] = cell
    for arm in arms:
        key = f"{arm}_correct"
        vals = [r[key] for r in rows if key in r]
        if vals:
            out[arm] = round(sum(vals) / len(vals), 3)
        ms = sorted(r[f"{arm}_ms"] for r in rows if f"{arm}_ms" in r)
        if ms:
            out[f"{arm}_ms_p50"] = ms[len(ms) // 2]
    out["write_ms_p50"] = sorted(r["write_ms"] for r in rows)[len(rows) // 2] if rows else 0
    return out


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    tasks = [
        build_task(pairs=args.pairs, level=lv, seed=args.seed * 1000 + lv * 100 + o)
        for lv in levels
        for o in range(args.orderings)
    ]
    ask_json = json.loads(args.ask_json) if args.ask_json else {}
    search_json = json.loads(args.search_json) if args.search_json else {}
    rows: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=180.0) as client:

        async def _one(task: dict[str, Any]) -> None:
            async with sem:
                try:
                    rows.append(
                        await run_task(
                            client,
                            task,
                            base_url=args.base_url.rstrip("/"),
                            token=args.token,
                            org=args.org,
                            arms=arms,
                            top_k=args.top_k,
                            ask_json=ask_json,
                            search_json=search_json,
                            write_mode=args.write_mode,
                            keep=args.keep_namespaces,
                        )
                    )
                except Exception as e:  # noqa: BLE001 — one bad task must not sink the run
                    print(
                        f"task level={task['level']} seed={task['seed']} failed: {e}",
                        file=sys.stderr,
                    )

        # Erased on exit, after a failure or Ctrl-C too.
        async with erase_on_exit(
            lambda p, b: _post(client, args.base_url.rstrip("/") + p, b, args.token),
            org=args.org,
            keep=args.keep_namespaces,
        ):
            await asyncio.gather(*[_one(t) for t in tasks])
        summary = summarize(rows, arms)
        summary["arms"] = arms
        summary["pairs"] = args.pairs
        print(json.dumps(summary, indent=2))
        if args.out:
            with open(args.out, "w") as f:
                for r in sorted(rows, key=lambda r: (r["level"], r["seed"])):
                    f.write(json.dumps(r) + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default=os.environ.get("LOOM_ORG", "dev"))
    ap.add_argument("--pairs", type=int, default=140)
    ap.add_argument("--levels", default="0,1,2,3,4")
    ap.add_argument("--orderings", type=int, default=3, help="tasks per level (published task: 30)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--arms", default="ask,search_k,chain")
    ap.add_argument("--write-mode", choices=["set", "extract"], default="set")
    ap.add_argument("--top-k", type=int, default=10, help="K for ask and search_k")
    ap.add_argument(
        "--ask-json", default="", help='extra memory.ask fields, e.g. \'{"depth":"raw"}\''
    )
    ap.add_argument("--search-json", default="", help="extra memory.search fields")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--out", default="", help="per-task JSONL")
    ap.add_argument("--keep-namespaces", action="store_true")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
