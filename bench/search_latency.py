"""memory.search latency by namespace size band, with the per-stage breakdown.

Every `memory.search` response carries `retrieval_work.stage_ms` (wall time per handler
stage) and `retrieval_work.loop_lag_ms` (one event-loop scheduling sample). This bench
issues real queries at real namespaces, groups the results by how many rows the
namespace holds, and reports p50/p95 wall time per band next to the mean stage breakdown
— so "search is slow" becomes "the rerank stage is slow at 10k–100k rows" or "the event
loop is saturated", which are different fixes.

Two ways to pick targets:

  --namespaces ns1,ns2 --queries queries.txt
      explicit: every query against every namespace, `--repeat` times.

  --from-trace N
      sample from the deployment's own traffic: for each size band, the N
      namespaces with the most searches in the last 7 days, and up to
      `--queries-per-ns` of their distinct queries from loom.search_trace_log.
      Needs LOOM_CH_* in the environment (or a dotenv named by --env-file);
      the ClickHouse read is read-only and never touches loom.* rows.

The server measured should be an isolated one (an iso uvicorn from the
branch under test) — never the shared deployment's process, whose queue depth is
part of what this measures. `--warm 1` sends each query once first and
discards it, so embedding-cache misses are not counted as retrieval.

    python bench/search_latency.py --base-url http://127.0.0.1:7821 \
        --token "$LOOM_API_KEY" --from-trace 2 --queries-per-ns 10 --repeat 2 \
        --out bench/search_latency.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BANDS: tuple[tuple[str, int], ...] = (("<1k", 1_000), ("1k-10k", 10_000), ("10k-100k", 100_000))


def band_for(rows: int | None) -> str:
    """Size band label for a namespace row count; None = unknown."""
    if rows is None:
        return "unknown"
    for label, upper in BANDS:
        if rows < upper:
            return label
    return "100k+"


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return float(xs[k])


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-band summary of bench rows: calls, p50/p95 wall, mean loop lag, and the
    mean of every stage seen (columns `stage_<name>_ms`), largest stage first."""
    by_band: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("status") != 200:
            continue
        by_band.setdefault(str(r.get("band", "unknown")), []).append(r)
    out: list[dict[str, Any]] = []
    for band, group in sorted(by_band.items(), key=lambda kv: kv[0]):
        walls = [float(r["wall_ms"]) for r in group]
        stage_sums: dict[str, float] = {}
        stage_n: dict[str, int] = {}
        for r in group:
            for k, v in (r.get("stage_ms") or {}).items():
                stage_sums[k] = stage_sums.get(k, 0.0) + float(v)
                stage_n[k] = stage_n.get(k, 0) + 1
        stages = {k: round(stage_sums[k] / stage_n[k], 1) for k in stage_sums}
        row: dict[str, Any] = {
            "band": band,
            "calls": len(group),
            "namespaces": len({str(r.get("namespace")) for r in group}),
            "p50_ms": round(_p(walls, 0.50), 1),
            "p95_ms": round(_p(walls, 0.95), 1),
            "mean_loop_lag_ms": round(
                statistics.mean(float(r.get("loop_lag_ms") or 0.0) for r in group), 2
            ),
            "mean_server_total_ms": round(stages.pop("total", 0.0), 1),
        }
        for k in sorted(stages, key=lambda k: -stages[k]):
            row[f"stage_{k}_ms"] = stages[k]
        out.append(row)
    return out


def _ch_client(env_file: str | None) -> Any:
    env = dict(os.environ)
    if env_file and not Path(env_file).exists():
        raise SystemExit(f"--env-file {env_file} does not exist")
    if env_file:
        for line in Path(env_file).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'").strip('"'))
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=env["LOOM_CH_HOST"],
        port=int(env.get("LOOM_CH_PORT", "8443")),
        username=env["LOOM_CH_USER"],
        password=env["LOOM_CH_PASSWORD"],
        database=env.get("LOOM_CH_DATABASE", "loom"),
        secure=True,
        settings={"readonly": 1, "max_execution_time": 60},
    )


def namespace_sizes(ch: Any, targets: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    if not targets:
        return {}
    res = ch.query(
        """
        SELECT org, namespace, count() AS rows
        FROM loom.memory_embeddings FINAL
        WHERE tombstoned_at IS NULL AND (org, namespace) IN {t:Array(Tuple(String, String))}
        GROUP BY org, namespace
        """,
        parameters={"t": targets},
    )
    return {(str(o), str(n)): int(r) for o, n, r in res.result_rows}


def targets_from_trace(
    ch: Any, per_band: int, queries_per_ns: int, org: str | None
) -> list[dict[str, Any]]:
    """[{org, namespace, rows, band, queries:[...]}] — the busiest namespaces per
    band over the last 7 days of search_trace_log, with their distinct queries."""
    org_pred = "AND a.org = {org:String}" if org else ""
    res = ch.query(
        f"""
        WITH sizes AS (
            SELECT org, namespace, count() AS rows FROM loom.memory_embeddings FINAL
            WHERE tombstoned_at IS NULL GROUP BY org, namespace),
        act AS (
            SELECT org, namespace, count() AS searches FROM loom.search_trace_log
            WHERE ts > now() - INTERVAL 7 DAY AND query != '' GROUP BY org, namespace)
        SELECT multiIf(rows < 1000, '<1k', rows < 10000, '1k-10k',
                       rows < 100000, '10k-100k', '100k+') AS band,
               a.org, a.namespace, rows, searches
        FROM act a INNER JOIN sizes s ON a.org = s.org AND a.namespace = s.namespace
        WHERE 1 = 1 {org_pred}
        ORDER BY band, searches DESC
        LIMIT {{n:UInt32}} BY band
        """,
        parameters={"n": per_band, "org": org or ""},
    )
    out: list[dict[str, Any]] = []
    for band, o, ns, rows, _searches in res.result_rows:
        qres = ch.query(
            """
            SELECT query FROM loom.search_trace_log
            WHERE ts > now() - INTERVAL 7 DAY AND org = {org:String} AND namespace = {ns:String}
              AND query != ''
            GROUP BY query ORDER BY count() DESC, query LIMIT {k:UInt32}
            """,
            parameters={"org": o, "ns": ns, "k": queries_per_ns},
        )
        queries = [str(r[0]) for r in qres.result_rows]
        if queries:
            out.append(
                {
                    "org": str(o),
                    "namespace": str(ns),
                    "rows": int(rows),
                    "band": str(band),
                    "queries": queries,
                }
            )
    return out


async def _search(
    client: httpx.AsyncClient, base: str, token: str, body: dict[str, Any]
) -> tuple[int, float, dict[str, Any]]:
    t0 = time.perf_counter()
    r = await client.post(
        f"{base}/v1/memory.search", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    wall = (time.perf_counter() - t0) * 1000.0
    try:
        j = r.json() if r.status_code == 200 else {}
    except ValueError:
        j = {}
    return r.status_code, wall, j


async def run(args: argparse.Namespace) -> int:
    targets: list[dict[str, Any]]
    ch = None
    if args.from_trace > 0:
        ch = _ch_client(args.env_file)
        targets = targets_from_trace(ch, args.from_trace, args.queries_per_ns, args.org or None)
    else:
        if not args.namespaces or not args.queries:
            print("pass --namespaces and --queries, or --from-trace N", file=sys.stderr)
            return 2
        queries = [q.strip() for q in Path(args.queries).read_text().splitlines() if q.strip()]
        nss = [n.strip() for n in args.namespaces.split(",") if n.strip()]
        sizes: dict[tuple[str, str], int] = {}
        try:
            ch = _ch_client(args.env_file)
            sizes = namespace_sizes(ch, [(args.org, n) for n in nss])
        except Exception as e:  # noqa: BLE001 — sizes are a label, not the measurement
            print(f"namespace sizes unavailable ({e}); band=unknown", file=sys.stderr)
        targets = [
            {
                "org": args.org,
                "namespace": n,
                "rows": sizes.get((args.org, n)),
                "band": band_for(sizes.get((args.org, n))),
                "queries": queries,
            }
            for n in nss
        ]
    if not targets:
        print("no targets", file=sys.stderr)
        return 2
    search_json = json.loads(args.search_json) if args.search_json else {}
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
        for t in targets:
            for q in t["queries"]:
                body = {
                    "org": t["org"],
                    "namespace": t["namespace"],
                    "agent": args.agent,
                    "query": q,
                    "top_k": args.top_k,
                    "namespace_scope": "this",
                    "auto_touch": False,
                    **({"search_mode": args.search_mode} if args.search_mode else {}),
                    **search_json,
                }
                for _ in range(args.warm):
                    await _search(client, args.base_url, args.token, body)
                for rep in range(args.repeat):
                    status, wall, j = await _search(client, args.base_url, args.token, body)
                    work = j.get("retrieval_work") or {}
                    row = {
                        "org": t["org"],
                        "namespace": t["namespace"],
                        "rows": t.get("rows"),
                        "band": t["band"],
                        "query": q,
                        "repeat": rep,
                        "status": status,
                        "wall_ms": round(wall, 1),
                        "results": len(j.get("results") or []),
                        "loop_lag_ms": work.get("loop_lag_ms"),
                        "stage_ms": work.get("stage_ms") or {},
                        "llm_calls_total": int(work.get("llm_calls_total") or 0),
                    }
                    rows.append(row)
                    if args.verbose:
                        print(
                            f"{t['band']:9s} {t['namespace'][:28]:28s} {status} "
                            f"{wall:7.1f} ms  {row['stage_ms']}",
                            flush=True,
                        )
    summary = summarize(rows)
    if args.out:
        out = Path(args.out)
        keys = [
            "org",
            "namespace",
            "rows",
            "band",
            "query",
            "repeat",
            "status",
            "wall_ms",
            "results",
            "loop_lag_ms",
            "llm_calls_total",
            "stage_ms",
        ]
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow(
                    {
                        **{k: r.get(k) for k in keys},
                        "stage_ms": json.dumps(r["stage_ms"], sort_keys=True),
                    }
                )
        Path(str(out) + ".summary.json").write_text(json.dumps(summary, indent=1))
    print(
        f"\nmemory.search latency against {args.base_url}: {len(rows)} calls, "
        f"{len({(r['org'], r['namespace']) for r in rows})} namespaces\n"
    )
    for s in summary:
        stages = ", ".join(f"{k[6:-3]}={v}" for k, v in s.items() if k.startswith("stage_"))
        print(
            f"{s['band']:9s} n={s['calls']:3d} ns={s['namespaces']} "
            f"p50={s['p50_ms']:7.1f} p95={s['p95_ms']:7.1f} "
            f"server={s['mean_server_total_ms']:7.1f} "
            f"loop_lag={s['mean_loop_lag_ms']:5.2f}\n          stages(ms): {stages}"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base-url", default="http://127.0.0.1:7777")
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="dev")
    ap.add_argument("--agent", default="latency-bench")
    ap.add_argument("--namespaces", default="", help="comma-separated; with --queries")
    ap.add_argument("--queries", default="", help="file, one query per line; with --namespaces")
    ap.add_argument(
        "--from-trace",
        type=int,
        default=0,
        help="N busiest namespaces per size band from search_trace_log (7d)",
    )
    ap.add_argument("--queries-per-ns", type=int, default=10)
    ap.add_argument(
        "--env-file",
        default="",
        help="dotenv with LOOM_CH_* for sizes / --from-trace; default: the environment only",
    )
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--search-mode", default="", help="empty = server default")
    ap.add_argument("--search-json", default="", help="extra memory.search fields as JSON")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--warm", type=int, default=1, help="discarded warm-up calls per query")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
