"""Wrong-abstain replay for code.grep.

For the trailing window, take every literal-grep row of `loom.code_search_trace`
(abstains by default, successes too with --include-successes) per
(org, namespace, consumer), recount every abstained (pattern, path_prefix)
against the live chunks in ONE scan per namespace (`multiSearchAllPositions`,
≤ 255 needles a call, batched), and print `wrong_abstain = served 0 ∧
corpus_in_scope > 0` per consumer with the patterns. Read-only against
ClickHouse.

With --base-url the same calls are replayed against a server (an isolated
one — never the shared deployment) with the ledger's own `pattern`,
`path_prefix`, `limit` (top_k) and mode (`grep_literal_files` → mode=files),
`case_sensitive` left at its default (the ledger does not record it), and the
table gains what that server served: `wrong_now` (served 0 there while the
corpus holds the literal in scope), the served-count range over --rounds,
p50/p90 latency, and for successes whether the served FILE list equals the
ledger's `returned_paths` (`files_identical`) or at least contains it
(`files_covered` — the honest check for a row written without its scope
and for an index that moved since). --rounds and --concurrency mirror the
consumer's fan-out (it sends the same grep to several namespaces inside 10 s).

    python bench/code_grep_replay.py --hours 168 --consumer clickgap
    python bench/code_grep_replay.py --hours 168 --consumer clickgap \\
        --base-url http://127.0.0.1:7875 --token-file scratch/bench.token \\
        --rounds 3 --concurrency 4 --include-successes --json out.json

House conventions: standalone argparse+httpx, the pure fold shared with the
gauge in server/observability/grep_replay_probe.py, every statement
analyzer-tested in tests/integration/test_search_statements_pass_analyzer.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench._http import apost
from server.observability import grep_replay_probe as probe

LEDGER_ROWS_SQL = """
SELECT ts, org, namespace, consumer, query AS pattern, route, top_k, result_count,
       abstain, latency_ms, returned_paths, chunks_matched, path_prefix
FROM loom.code_search_trace
WHERE ts > now() - INTERVAL {hours:UInt32} HOUR
  AND op = 'code.grep'
  AND route IN ('grep_literal', 'grep_literal_files',
                'grep_literal+prefix', 'grep_literal+prefix_files')
  AND length(query) >= 2
  AND (abstain = 1 OR {successes:UInt8} = 1)
  AND ({org:String} = '' OR org = {org:String})
  AND ({ns:String} = '' OR namespace = {ns:String})
  AND ({consumer:String} = '' OR consumer = {consumer:String})
ORDER BY ts
"""

ReplayKey = tuple[str, str, str, str, str, str]  # org, ns, pattern, prefix, mode, limit


def _client(env_file: str | None) -> Any:
    env = dict(os.environ)
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                if v[:1] not in "{\"'" and " #" in v:
                    v = v.split(" #", 1)[0].rstrip()
                env.setdefault(k.strip(), v.strip("'").strip('"'))
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=env["LOOM_CH_HOST"],
        port=int(env.get("LOOM_CH_PORT", "8443")),
        username=env["LOOM_CH_USER"],
        password=env["LOOM_CH_PASSWORD"],
        database=env.get("LOOM_CH_DATABASE", "loom"),
        secure=env.get("LOOM_CH_SECURE", "true").lower() != "false",
        settings={"readonly": 1, "max_execution_time": 180},
    )


def ledger_rows(
    ch: Any, *, hours: int, org: str, ns: str, consumer: str, successes: bool
) -> list[dict[str, Any]]:
    """Per-call ledger rows."""
    res = ch.query(
        LEDGER_ROWS_SQL,
        parameters={
            "hours": int(hours),
            "successes": 1 if successes else 0,
            "org": org,
            "ns": ns,
            "consumer": consumer,
        },
    )
    rows = [dict(zip(res.column_names, r, strict=False)) for r in res.result_rows]
    for r in rows:
        r["ts"] = str(r["ts"])
        r["path_prefix"] = str(r.get("path_prefix") or "")
        r["mode"] = "files" if str(r["route"]).endswith("_files") else "matches"
    return rows


def recount_all(
    ch: Any, rows: list[dict[str, Any]]
) -> dict[tuple[str, str], dict[probe.Pair, int]]:
    by_ns: dict[tuple[str, str], list[probe.Pair]] = {}
    for r in rows:
        by_ns.setdefault((str(r["org"]), str(r["namespace"])), []).append(
            (str(r["pattern"]), str(r["path_prefix"]))
        )
    return {key: probe.recount(ch, key[0], key[1], pairs) for key, pairs in by_ns.items()}


def _call_body(r: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "org": r["org"],
        "namespace": r["namespace"],
        "pattern": r["pattern"],
        "mode": r["mode"],
        "consumer": "grep-replay",
    }
    if r.get("path_prefix"):
        body["path_prefix"] = r["path_prefix"]
    if int(r.get("top_k") or 0) > 0:
        body["limit"] = int(r["top_k"])
    return body


def served_files(payload: dict[str, Any], mode: str) -> list[str]:
    """The FILE list the trace writer records for a response: distinct paths
    in first-seen order, capped at 50 like `returned_paths`."""
    if mode == "files":
        paths = [str(f.get("path") or "") for f in (payload.get("files") or [])]
    else:
        paths = [str(m.get("path") or "") for m in (payload.get("matches") or [])]
    return list(dict.fromkeys(paths))[:50]


def _key(r: dict[str, Any]) -> ReplayKey:
    return (
        str(r["org"]),
        str(r["namespace"]),
        str(r["pattern"]),
        str(r.get("path_prefix") or ""),
        r["mode"],
        str(r.get("top_k") or ""),
    )


def _consumer_key(r: dict[str, Any]) -> tuple[str, str, str]:
    return (str(r["org"]), str(r["namespace"]), str(r["consumer"]))


async def replay(
    rows: list[dict[str, Any]], *, base_url: str, token: str, rounds: int, concurrency: int
) -> dict[ReplayKey, dict[str, Any]]:
    """Replay every distinct call shape `rounds` times; returns per key: served
    counts per round, chunks_matched per round, latencies, errors, and the
    served file list of the last round."""
    import httpx

    keys: dict[ReplayKey, dict[str, Any]] = {}
    for r in rows:
        keys.setdefault(
            _key(r), {"row": r, "served": [], "chunks": [], "ms": [], "errors": 0, "files": []}
        )
    sem = asyncio.Semaphore(max(1, concurrency))
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def one(client: Any, entry: dict[str, Any]) -> None:
        async with sem:
            t = time.monotonic()
            try:
                resp = await apost(client, "/v1/code.grep", _call_body(entry["row"]), waits_429=7)
            except Exception:  # noqa: BLE001 — an errored call is counted, not fatal
                entry["errors"] += 1
                return
            entry["ms"].append(1000.0 * (time.monotonic() - t))
            payload = resp.json()
            mode = entry["row"]["mode"]
            served = len(payload.get("files" if mode == "files" else "matches") or [])
            entry["served"].append(int(served))
            entry["chunks"].append(int(payload.get("chunks_matched") or 0))
            entry["files"] = served_files(payload, mode)

    async with httpx.AsyncClient(base_url=base_url, timeout=60.0, headers=headers) as client:
        for _ in range(max(1, rounds)):
            await asyncio.gather(*(one(client, e) for e in keys.values()))
    return keys


def report(
    rows: list[dict[str, Any]],
    corpus: dict[tuple[str, str], dict[probe.Pair, int]],
    replayed: dict[ReplayKey, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Pure: the per-consumer table and the per-call detail."""
    abstains = [r for r in rows if int(r.get("abstain") or 0) == 1]
    folded = probe.fold(
        [(*_consumer_key(r), str(r["pattern"]), str(r["path_prefix"]), 1) for r in abstains],
        corpus,
    )
    for f in folded:
        fk = (f["org"], f["namespace"], f["consumer"])
        f["calls"] = sum(1 for r in rows if _consumer_key(r) == fk)
    detail: list[dict[str, Any]] = []
    identical = covered = total_success = wrong_now = 0
    lat: list[float] = []
    for r in rows:
        key = _key(r)
        ns_key = (str(r["org"]), str(r["namespace"]))
        hits = int(corpus.get(ns_key, {}).get((str(r["pattern"]), str(r["path_prefix"])), 0))
        abstained = int(r.get("abstain") or 0) == 1
        d: dict[str, Any] = {
            "ts": r["ts"],
            "consumer": r["consumer"],
            "namespace": r["namespace"],
            "pattern": r["pattern"],
            "path_prefix": r["path_prefix"],
            "mode": r["mode"],
            "limit": r.get("top_k"),
            "ledger_served": int(r.get("result_count") or 0),
            "ledger_abstain": int(abstained),
            "ledger_chunks_matched": r.get("chunks_matched"),
            "ledger_latency_ms": int(r.get("latency_ms") or 0),
            "corpus_in_scope": hits,
            "wrong_abstain": abstained and hits > 0,
        }
        if replayed is not None and key in replayed:
            e = replayed[key]
            d["served_now"] = [min(e["served"]), max(e["served"])] if e["served"] else None
            d["chunks_now"] = [min(e["chunks"]), max(e["chunks"])] if e["chunks"] else None
            d["errors_now"] = e["errors"]
            lat.extend(e["ms"])
            if e["served"] and min(e["served"]) == 0 and hits > 0:
                d["wrong_now"] = True
                wrong_now += 1
            if not abstained and e["served"]:
                total_success += 1
                ledger_paths = [str(p) for p in (r.get("returned_paths") or [])]
                same = ledger_paths == e["files"]
                sub = set(ledger_paths) <= set(e["files"])
                d["files_identical"] = same
                d["files_covered"] = sub
                identical += int(same)
                covered += int(sub)
        detail.append(d)
    out: dict[str, Any] = {"consumers": folded, "calls": detail}
    if replayed is not None:
        out["replay"] = {
            "wrong_now": wrong_now,
            "success_files_identical": [identical, total_success],
            "success_files_covered": [covered, total_success],
            "p50_ms": round(statistics.median(lat), 1) if lat else None,
            "p90_ms": round(sorted(lat)[int(len(lat) * 0.9)], 1) if lat else None,
            "calls": len(lat),
        }
    return out


def print_report(rep: dict[str, Any]) -> None:
    print(f"{'org':10} {'namespace':22} {'consumer':16} {'calls':>6} {'abstain':>8} {'wrong':>6}")
    for f in rep["consumers"]:
        print(
            f"{f['org']:10} {f['namespace']:22} {f['consumer']:16} {f['calls']:>6} "
            f"{f['abstain_calls']:>8} {f['wrong_calls']:>6}"
        )
        for p in f["wrong_patterns"]:
            pre = f" prefix={p['path_prefix']!r}" if p["path_prefix"] else ""
            print(f"    wrong: {p['pattern']!r:44} calls={p['calls']:<3} corpus={p['corpus']}{pre}")
    if "replay" in rep:
        r = rep["replay"]
        same, total = r["success_files_identical"]
        cov = r["success_files_covered"][0]
        print(
            f"replay: calls={r['calls']} wrong_now={r['wrong_now']} "
            f"success_files_identical={same}/{total} covered={cov}/{total} "
            f"p50={r['p50_ms']} ms p90={r['p90_ms']} ms"
        )
        for d in rep["calls"]:
            if d.get("wrong_now"):
                print(
                    f"    WRONG NOW: {d['pattern']!r} prefix={d['path_prefix']!r} "
                    f"corpus={d['corpus_in_scope']} served_now={d['served_now']}"
                )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument(
        "--env-file",
        default="",
        help="a .env to read LOOM_CH_* from (none by default: the process environment)",
    )
    ap.add_argument("--org", default="")
    ap.add_argument("--namespace", default="")
    ap.add_argument("--consumer", default="")
    ap.add_argument("--include-successes", action="store_true")
    ap.add_argument("--base-url", default="", help="replay against this server (an isolated one)")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--json", default="", help="write the full report here")
    args = ap.parse_args()

    ch = _client(args.env_file)
    rows = ledger_rows(
        ch,
        hours=args.hours,
        org=args.org,
        ns=args.namespace,
        consumer=args.consumer,
        successes=args.include_successes,
    )
    corpus = recount_all(ch, rows)
    replayed = None
    if args.base_url:
        token = Path(args.token_file).read_text().strip() if args.token_file else ""
        replayed = asyncio.run(
            replay(
                rows,
                base_url=args.base_url,
                token=token,
                rounds=args.rounds,
                concurrency=args.concurrency,
            )
        )
    rep = report(rows, corpus, replayed)
    print_report(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
