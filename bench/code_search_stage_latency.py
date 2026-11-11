"""code.search latency by stage — read-only, re-runnable.

Two modes:

  table   the per-stage distribution from `loom.code_search_trace.stage_ms`
          (a Map of exclusive milliseconds per stage) for all
          consumers and for one consumer, plus the request wall from
          `loom.agent_events` beside the summed ClickHouse server time
          (`args['out_ch_elapsed_ms']`):

            python bench/code_search_stage_latency.py table --days 3

  live    the service's `search()` in this process against the deployment's
          ClickHouse and embedder (LOOM_ENV_FILE names the .env), on a consumer's recent
          plain-text queries, with every CodeRepo statement timed twice — the
          client wall and the server's own `elapsed_ns` — and every provider
          embed timed on its own, so "is the vector stage ClickHouse or the
          embedder" is answered per statement, not guessed:

            python bench/code_search_stage_latency.py live --n 20 \
                [--llm-legs auto|none] [--deadline-ms 0]

Nothing is written: the shared embedding-cache put is stubbed out and no
trace / memo row is scheduled (those live in the handler, not the service).
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import functools
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _load_env() -> None:
    env_file = os.environ.get("LOOM_ENV_FILE", "")  # no host default
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if v[:1] not in "{\"'" and " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            if v[:1] in "\"'" and v[-1:] == v[:1]:
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)
    os.environ["LOOM_BACKGROUND_WORKERS"] = "off"
    sys.path.insert(0, str(HERE.parent))


def _q(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def _print_table(title: str, rows: dict[str, list[float]]) -> None:
    print(f"\n{title}")
    print(f"{'stage':<28}{'n':>6}{'p50':>9}{'p90':>9}{'p99':>9}")
    for name, vals in sorted(rows.items(), key=lambda kv: -_q(kv[1], 0.9)):
        p50, p90, p99 = _q(vals, 0.5), _q(vals, 0.9), _q(vals, 0.99)
        print(f"{name:<28}{len(vals):>6}{p50:>9.0f}{p90:>9.0f}{p99:>9.0f}")


_STAGE_SQL = """
SELECT if(consumer = {c:String}, {c:String}, 'all') AS who, k AS stage,
       count() AS n, quantile(0.5)(v) AS p50, quantile(0.9)(v) AS p90, quantile(0.99)(v) AS p99
FROM loom.code_search_trace ARRAY JOIN mapKeys(stage_ms) AS k, mapValues(stage_ms) AS v
WHERE ts > now() - toIntervalDay({days:UInt32}) AND op = 'code.search'
GROUP BY GROUPING SETS ((who, stage), (stage))
ORDER BY who, p90 DESC
"""

_WALL_SQL = """
SELECT consumer = {c:String} AS is_c, count() AS n,
       quantiles(0.5, 0.9, 0.99)(latency_ms) AS wall,
       quantiles(0.5, 0.9, 0.99)(toFloat64OrZero(args['out_ch_elapsed_ms'])) AS ch_sum,
       quantile(0.5)(toUInt32OrZero(args['out_ch_statements'])) AS statements
FROM loom.agent_events
WHERE operation = 'code.search' AND ts > now() - toIntervalDay({days:UInt32})
GROUP BY is_c ORDER BY is_c
"""


async def _table(args: argparse.Namespace) -> None:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    p = {"c": args.consumer, "days": args.days}
    rows = await ch.query_all(_STAGE_SQL, p)
    for who in ("", "all", args.consumer):
        label = {"": "every consumer", "all": f"every consumer but {args.consumer}"}.get(who, who)
        print(f"\ncode.search stage_ms, {label}, {args.days} d (exclusive ms per stage)")
        print(f"{'stage':<16}{'n':>7}{'p50':>8}{'p90':>8}{'p99':>8}")
        for r in rows:
            if r["who"] == who:
                print(
                    f"{r['stage']:<16}{r['n']:>7}{r['p50']:>8.0f}{r['p90']:>8.0f}{r['p99']:>8.0f}"
                )
    print(f"\nrequest wall vs summed ClickHouse server time (agent_events, {args.days} d)")
    for r in await ch.query_all(_WALL_SQL, p):
        who = args.consumer if r["is_c"] else "others"
        w, c = r["wall"], r["ch_sum"]
        print(
            f"{who:<10} n={r['n']:<6} wall p50/p90/p99 {w[0]:.0f}/{w[1]:.0f}/{w[2]:.0f} ms"
            f"  ch_sum {c[0]:.0f}/{c[1]:.0f}/{c[2]:.0f} ms  statements p50 {r['statements']}"
        )
    await ch.disconnect()


# ---- live -------------------------------------------------------------------

_ACC: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar("bench_acc", default=None)


def _instrument(stmts: list[tuple[str, float, float]], embeds: list[float]) -> None:
    """Time every CodeRepo coroutine (client wall) and the server elapsed of
    the statements it issued (child tasks inherit the accumulator), and every
    provider embed; stub the shared cache put (the one write on the path)."""
    import server.core.ch as chmod
    from server.code.store.index import CodeRepo
    from server.embedding import _cache

    for name in dir(CodeRepo):
        fn = getattr(CodeRepo, name)
        if name.startswith("_") or not asyncio.iscoroutinefunction(fn):
            continue

        def wrap(f: Any, n: str) -> Any:
            @functools.wraps(f)
            async def inner(*a: Any, **kw: Any) -> Any:
                if _ACC.get() is not None:  # nested repo call: the outer one owns it
                    return await f(*a, **kw)
                acc = [0.0]
                tok = _ACC.set(acc)
                t0 = time.perf_counter()
                try:
                    return await f(*a, **kw)
                finally:
                    stmts.append((n, (time.perf_counter() - t0) * 1000, acc[0]))
                    _ACC.reset(tok)

            return inner

        setattr(CodeRepo, name, wrap(fn, name))

    orig_note = chmod.note_result_summary

    def note(result: Any) -> dict[str, Any]:
        parsed = orig_note(result)
        acc = _ACC.get()
        if acc is not None:
            acc[0] += float(parsed.get("elapsed_ms") or 0)
        return parsed

    chmod.note_result_summary = note  # type: ignore[assignment]
    orig_embed = _cache._embed_uncached

    async def timed(model: str, k: str, text: str, fetch: Any, dim: int, org: str) -> Any:
        async def f(t: str) -> list[float]:
            t0 = time.perf_counter()
            try:
                return await fetch(t)
            finally:
                embeds.append((time.perf_counter() - t0) * 1000)

        return await orig_embed(model, k, text, f, dim, org)

    _cache._embed_uncached = timed  # type: ignore[assignment]
    _cache._shared_put = lambda *a, **kw: None  # type: ignore[assignment]


async def _live(args: argparse.Namespace) -> int:
    from server.code.app import search as code_search
    from server.code.app.spans import current_stage_ms, start_stage_wall
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.embedding.embedder import get_embedder
    from server.observability import genai
    from server.transport._common import begin_request

    stmts: list[tuple[str, float, float]] = []
    embeds: list[float] = []
    _instrument(stmts, embeds)
    ch = get_client()
    await ch.connect()
    from server.code.app.index.repo_registry import read_registry_or_exit

    await read_registry_or_exit("code_search_stage_latency")
    queries = [
        str(r["query"])
        for r in await ch.query_all(
            "SELECT query, max(ts) AS t FROM loom.code_search_trace "
            "WHERE op = 'code.search' AND (consumer = {c:String} OR {c:String} = '*') "
            "AND namespace = {ns:String} "
            "AND ts > now() - toIntervalDay({d1:UInt32}) "
            "AND ts <= now() - toIntervalDay({d0:UInt32}) "
            "AND NOT startsWith(query, 'sha256:') "
            "AND (NOT {hyde:Bool} OR mapContains(stage_ms, 'hyde_leg')) "
            "AND startsWith(route, {route:String}) "
            "GROUP BY query ORDER BY t DESC LIMIT {n:UInt32}",
            {
                "c": args.consumer,
                "ns": args.namespace,
                "n": args.n,
                "d0": args.skip_days,
                "d1": args.skip_days + args.days,
                "hyde": args.hyde_only,
                "route": args.route,
            },
        )
    ]
    if not queries:
        print("no code.search queries in the window for these filters", file=sys.stderr)
        await ch.disconnect()
        return 1
    embedder = get_embedder()
    stages: dict[str, list[float]] = defaultdict(list)
    walls: list[float] = []
    by_method: dict[str, list[tuple[float, float]]] = defaultdict(list)
    extra = {}
    if args.llm_legs:
        extra["llm_legs"] = args.llm_legs
    if args.deadline_ms:
        extra["deadline_ms"] = args.deadline_ms
    from server.observability.traffic_class import set_current_traffic_class

    handler: Any = None
    if args.handler:
        import server.code.app.search.handler as hmod
        from server.code.api.models_search import CodeSearchInput
        from server.code.app import search_memo as code_search_memo

        # read-only: no trace row, no memo row, no continuation pool row
        hmod.schedule_code_trace = lambda **kw: None  # type: ignore[assignment]
        hmod._issue_search_cursor = lambda *a, **kw: ""  # type: ignore[assignment]
        code_search_memo.enabled = lambda inp: False  # type: ignore[assignment]
        handler = hmod.handle_code_search

    for i, q in enumerate(queries):
        begin_request()
        # The embedding cache is scoped by the request's org, as a transport records it.
        genai.set_identity(org=args.org, namespace=args.namespace, operation="code.search")
        if args.traffic_class:
            # the class a real caller's token carries: its
            # per-statement `priority` / `max_threads` ride every read
            set_current_traffic_class(args.traffic_class, "bench")
        stmts.clear()
        t0 = time.perf_counter()
        if args.handler:
            # the whole op as the transport runs it (thresholds, memo probe,
            # risk annotation, as_of, coverage note) — its writes stubbed
            res = await handler(
                CodeSearchInput(
                    org=args.org,
                    namespace=args.namespace,
                    query=q,
                    top_k=5,
                    bodies="none",
                    consumer="latency-bench",  # a measurement label: never credits
                    **extra,
                )
            )
        else:
            start_stage_wall()
            res = await code_search.search(
                CodeRepo(),
                args.org,
                args.namespace,
                query=q,
                top_k=5,
                embedder=embedder,
                **extra,
            )
        wall = (time.perf_counter() - t0) * 1000
        walls.append(wall)
        units = res.results if args.handler else res.units
        for k, v in current_stage_ms().items():
            stages[k].append(float(v))
        for n, w, s in stmts:
            by_method[n].append((w, s))
        timed_out = getattr(res, "legs_timed_out", None) or []
        if args.dump:
            with open(args.dump, "a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "query": q,
                            "wall_ms": round(wall),
                            "timed_out": timed_out,
                            "units": [u.unit_id for u in units],
                        }
                    )
                    + "\n"
                )
        print(
            f"[{i + 1}/{len(queries)}] {wall:6.0f} ms route={res.route} "
            f"units={len(units)} timed_out={','.join(timed_out) or '-'} q={q[:60]!r}"
        )
    stages["(request wall)"] = walls
    _print_table("stage_ms (exclusive ms) in-process", stages)
    _print_table("provider embed calls (ms)", {"embed.provider": embeds})
    print(
        f"\n{'CodeRepo method':<34}{'n':>5}{'wall p50':>10}{'wall p90':>10}"
        f"{'server p50':>12}{'server p90':>12}"
    )
    for n, vals in sorted(by_method.items(), key=lambda kv: -_q([w for w, _ in kv[1]], 0.9)):
        w = [x for x, _ in vals]
        s = [y for _, y in vals]
        print(
            f"{n:<34}{len(vals):>5}{_q(w, 0.5):>10.0f}{_q(w, 0.9):>10.0f}"
            f"{_q(s, 0.5):>12.0f}{_q(s, 0.9):>12.0f}"
        )
    print(
        f"\nrequest wall p50 {statistics.median(walls):.0f} ms, p90 {_q(walls, 0.9):.0f} ms, "
        f"max {max(walls):.0f} ms over {len(walls)} queries"
    )
    await ch.disconnect()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    t = sub.add_parser("table")
    t.add_argument("--days", type=int, default=3)
    t.add_argument("--consumer", default="clickgap")
    lv = sub.add_parser("live")
    lv.add_argument("--n", type=int, default=20)
    lv.add_argument("--consumer", default="clickgap", help="'*' = every consumer")
    lv.add_argument("--org", default="default")
    lv.add_argument("--namespace", default="code-clickhouse")
    lv.add_argument("--days", type=int, default=7, help="query window length")
    lv.add_argument(
        "--skip-days",
        type=int,
        default=0,
        help="window ends this many days ago (older = colder caches)",
    )
    lv.add_argument(
        "--route",
        default="",
        help="only queries the trace served on this route (prefix: symbol_exact, hybrid, ...)",
    )
    lv.add_argument(
        "--hyde-only",
        action="store_true",
        help="only queries whose trace ran the HyDE leg (responsibility kind)",
    )
    lv.add_argument("--llm-legs", default="", choices=["", "auto", "none"])
    lv.add_argument(
        "--traffic-class",
        default="production",
        help="production | interactive | bench | '' (none)",
    )
    lv.add_argument(
        "--handler",
        action="store_true",
        help="run the whole op (handle_code_search), not only the service's search()",
    )
    lv.add_argument("--dump", default="", help="append one JSON line per query (ids, wall)")
    lv.add_argument("--deadline-ms", type=int, default=0)
    args = ap.parse_args()
    _load_env()
    if args.mode == "table":
        asyncio.run(_table(args))
        return 0
    return asyncio.run(_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
