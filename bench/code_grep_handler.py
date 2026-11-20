"""code.grep end to end, in process: handler wall and every statement it runs.

Replays real consumer `code.grep` calls (unredacted `loom.code_search_trace`
rows, matches mode by default) through `handle_code_grep` in this process,
against the ClickHouse the env file names. Every `CHClient.query_all` /
`query_one` is wrapped to record its first SELECT line, its wall and the
handler stage it ran in, so a call reads as `wall = sum(statements) + the
rest` with each statement named. Nothing is written: the trace row and the
continuation pool are no-ops here (they are fire-and-forget in the server
too, so the handler wall is unchanged by that).

Each call runs once, in the order the ledger saw them (no warm-up), with
the process-level caches as a deployment has them: `index_as_of` and the
ErrorCodes table are cached per namespace, so the first call on a namespace
pays their statements and later ones do not.

    python bench/code_grep_handler.py --per-mode 60
    python bench/code_grep_handler.py --modes matches,files,message
    python bench/code_grep_handler.py --cold --offset 360

Read-only (SELECTs).
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("bench_stage", default="?")

TRACE_SQL = """
SELECT multiIf(route LIKE '%message%', 'message', route LIKE '%\\_files', 'files', 'matches') AS m,
       org, namespace, path_prefix, query, top_k, args_json
FROM loom.code_search_trace
WHERE op = 'code.grep' AND ts > now() - INTERVAL {hours:UInt32} HOUR
  AND consumer = {consumer:String} AND redacted = 0 AND length(query) >= 2
  AND route NOT LIKE '%scoped%' AND route NOT LIKE 'validation%'
ORDER BY ts
"""


def load_env(path: str) -> None:
    """LOOM_* and provider keys from the env file, without overriding."""
    if not path or not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] not in "{\"'" and " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        os.environ.setdefault(k.strip(), v.strip("'").strip('"'))


def _head(sql: str) -> str:
    flat = " ".join(sql.split())
    m = re.search(r"FROM (loom\.\w+|\(|system\.\w+)", flat)
    table = m.group(1) if m else "?"
    return f"{table} :: {flat[:70]}"


def instrument(log: list[dict[str, Any]]) -> None:
    from server.core.ch import CHClient

    for name in ("query_all", "query_one"):
        orig = getattr(CHClient, name)

        async def wrapped(self, sql, *a, _orig=orig, **kw):  # type: ignore[no-untyped-def]
            t0 = time.monotonic()
            try:
                return await _orig(self, sql, *a, **kw)
            finally:
                log.append(
                    {
                        "stage": STAGE.get(),
                        "sql": _head(sql),
                        "ms": (time.monotonic() - t0) * 1000,
                        "t0": t0,
                    }
                )

        setattr(CHClient, name, wrapped)


def stage_spans() -> None:
    """Name the handler stages the statements run in (module functions wrapped)."""
    from server.code.app import common as cc
    from server.code.app import grep
    from server.code.app.search import handler as h

    def wrap(mod: Any, fn: str, label: str) -> None:
        orig = getattr(mod, fn)

        async def w(*a, _o=orig, _l=label, **kw):  # type: ignore[no-untyped-def]
            tok = STAGE.set(_l)
            try:
                return await _o(*a, **kw)
            finally:
                STAGE.reset(tok)

        setattr(mod, fn, w)

    wrap(grep, "_fetch_grep_chunks", "chunks")
    wrap(grep, "_unit_spans", "unit_spans")
    wrap(grep, "_grep_neighbours", "neighbours")
    wrap(h, "index_as_of", "as_of")
    wrap(cc, "index_as_of", "as_of")


def cold_reads() -> None:
    """--cold: every statement the handler runs reads object storage (the
    filesystem cache and the query condition cache off) — the worst case a
    replica restart or an evicted part hands a call."""
    import server.core.ch as chmod

    orig = chmod.statement_settings

    def cold(sql: str, extra: dict[str, Any] | None = None, cls: str | None = None) -> dict:
        out = orig(sql, extra, cls)
        out.update({"enable_filesystem_cache": 0, "use_query_condition_cache": 0})
        return out

    chmod.statement_settings = cold  # type: ignore[assignment]


def quiet_writes() -> None:
    from server.code.app import common as cc
    from server.code.app import search_cursor as paging
    from server.code.app.search import handler as h

    def noop(*a: Any, **k: Any) -> None:
        return None

    paging.put_pool = noop  # type: ignore[assignment]
    cc.schedule_code_trace = noop  # type: ignore[assignment]
    h.schedule_code_trace = noop  # type: ignore[assignment]


def _input(call: dict[str, Any]) -> Any:
    from server.code.api.models_grep import CodeGrepInput

    args = json.loads(call.get("args_json") or "{}") if call.get("args_json") else {}
    kw: dict[str, Any] = {
        "org": call["org"],
        "namespace": call["namespace"],
        "pattern": call["query"],
        "mode": call["m"],
        "path_prefix": call["path_prefix"] or "",
    }
    for k in ("regex", "case_sensitive", "limit", "neighbours"):
        if k in args and args[k] is not None:
            kw[k] = args[k]
    return CodeGrepInput(**kw)


async def run_calls(calls: list[dict[str, Any]], log: list[dict[str, Any]]) -> list[dict]:
    from server.code.app.grep import handle_code_grep

    out = []
    for call in calls:
        try:
            inp = _input(call)
        except Exception:  # noqa: BLE001 — a ledger row the model now refuses
            continue
        start = len(log)
        t0 = time.monotonic()
        try:
            res = await handle_code_grep(inp)
            err = ""
        except Exception as e:  # noqa: BLE001
            res, err = None, f"{type(e).__name__}: {e}"[:200]
        wall = (time.monotonic() - t0) * 1000
        stmts = log[start:]
        out.append(
            {
                "m": call["m"],
                "ns": call["namespace"],
                "pattern": call["query"],
                "wall": wall,
                "err": err,
                "stmts": stmts,
                "answer": res.model_dump(exclude={"trace_run_id", "next_cursor"}) if res else None,
            }
        )
    return out


def _q(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else 0.0


def report(recs: list[dict[str, Any]]) -> str:
    lines = []
    for mode in ("matches", "files", "message"):
        rs = [r for r in recs if r["m"] == mode and not r["err"]]
        if not rs:
            continue
        walls = [r["wall"] for r in rs]
        stages: dict[str, list[float]] = {}
        for r in rs:
            per: dict[str, float] = {}
            for s in r["stmts"]:
                per[s["stage"]] = per.get(s["stage"], 0.0) + s["ms"]
            per["(not SQL)"] = r["wall"] - sum(s["ms"] for s in r["stmts"])
            for k, v in per.items():
                stages.setdefault(k, []).append(v)
        lines.append(
            f"{mode}: calls {len(rs)}  wall p50/p95/p99 {_q(walls, 0.5):.0f} / "
            f"{_q(walls, 0.95):.0f} / {_q(walls, 0.99):.0f} ms  max {max(walls):.0f}  "
            f"statements p50 {_q([len(r['stmts']) for r in rs], 0.5):.0f}"
        )
        for k, v in sorted(stages.items()):
            v = v + [0.0] * (len(rs) - len(v))
            lines.append(
                f"    {k:<12} calls with it {sum(1 for x in v if x):>4}  "
                f"p50/p95/p99 {_q(v, 0.5):.0f} / {_q(v, 0.95):.0f} / {_q(v, 0.99):.0f} ms"
            )
        heads: dict[str, list[float]] = {}
        for r in rs:
            for s in r["stmts"]:
                heads.setdefault(f"[{s['stage']}] {s['sql']}", []).append(s["ms"])
        for k, v in sorted(heads.items(), key=lambda kv: -sum(kv[1]))[:8]:
            lines.append(f"      {len(v):>4}x p50 {_q(v, 0.5):>6.0f} p99 {_q(v, 0.99):>6.0f}  {k}")
        errs = [r for r in recs if r["m"] == mode and r["err"]]
        if errs:
            lines.append(f"    errors: {len(errs)} e.g. {errs[0]['err']}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--env-file",
        default="",
        help="a .env to read LOOM_CH_* from (none by default: the process environment)",
    )
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--consumer", default="clickgap")
    ap.add_argument("--modes", default="matches")
    ap.add_argument("--per-mode", type=int, default=60)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--json", default="")
    ap.add_argument("--cold", action="store_true", help="filesystem + condition caches off")
    args = ap.parse_args()
    load_env(args.env_file)
    from server.settings import settings

    object.__setattr__(settings, "code_intel_enabled", True)
    object.__setattr__(settings, "code_trace_log_enabled", False)
    log: list[dict[str, Any]] = []
    instrument(log)
    stage_spans()
    quiet_writes()
    if args.cold:
        cold_reads()

    async def go() -> list[dict[str, Any]]:
        from server.code.app.index.repo_registry import read_registry_or_exit
        from server.core.ch import get_client

        await get_client().connect()
        await read_registry_or_exit("code_grep_handler")
        rows = await get_client().query_all(
            TRACE_SQL, {"hours": args.hours, "consumer": args.consumer}
        )
        log.clear()
        by: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, ...]] = set()
        for r in rows:
            key = (str(r["m"]), str(r["namespace"]), str(r["query"]), str(r["path_prefix"]))
            if key in seen:
                continue
            seen.add(key)
            by.setdefault(str(r["m"]), []).append(dict(r))
        window = slice(args.offset, args.offset + args.per_mode)
        calls = [c for m in args.modes.split(",") for c in by.get(m, [])[window]]
        return await run_calls(calls, log)

    recs = asyncio.run(go())
    print(report(recs))
    if args.json:
        Path(args.json).write_text(json.dumps(recs, default=str))


if __name__ == "__main__":
    main()
