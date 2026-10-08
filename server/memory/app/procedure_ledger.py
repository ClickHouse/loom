"""Procedure run ledger — one append-only row per reported execution.

`procedure.record_run` inserts here instead of rewriting the procedure row;
`procedure.evolve` folds the rows newer than the procedure's
`ledger_folded_through` watermark into the graph's statistics with the pure
`merge_run`, then advances the watermark. The row keeps the folded
counters, so `procedure.match` / `procedure.locate` never query this table
on the read path; it is the source of truth for history and for SQL over
"which transitions fail most this week":

    SELECT edge.1 AS src, edge.2 AS dst, countIf(NOT ok) AS fails, count() AS runs
    FROM loom.procedure_runs
    ARRAY JOIN arrayZip(arrayPopBack(path), arrayPopFront(path)) AS edge
    WHERE namespace = 'ns' AND memory_key = 'procedure_x' AND ts > now() - INTERVAL 7 DAY
    GROUP BY src, dst ORDER BY fails DESC

Every function here raises on a ClickHouse failure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now as _now_ts

TABLE = "loom.procedure_runs"
EPOCH = "1970-01-01 00:00:00.000"


def format_ts(ts: datetime) -> str:
    """Watermark format: what ClickHouse prints for DateTime64(3)."""
    return ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


async def record(
    *,
    org: str,
    namespace: str,
    memory_key: str,
    agent: str,
    session_id: str,
    path: list[str],
    ok: bool,
    note: str,
    revision: int,
    steps_taken: int = 0,
    duration_ms: int = 0,
    tokens: int = 0,
) -> datetime:
    """Append one run. Synchronous insert so the row is visible to the
    count that follows it. Returns the timestamp written. The cost columns
    are 0 when the caller did not report them."""
    from server.core.ch import get_client

    ts = _now_ts()
    await get_client().insert(
        TABLE,
        [
            {
                "org": org,
                "namespace": namespace,
                "memory_key": memory_key,
                "agent": (agent or "")[:128],
                "session_id": (session_id or "")[:128],
                "path": [str(p)[:64] for p in path],
                "ok": bool(ok),
                "note": (note or "")[:512],
                "revision": max(0, int(revision)),
                "ts": ts,
                "steps_taken": min(65535, max(0, int(steps_taken or 0))),
                "duration_ms": min(4_294_967_295, max(0, int(duration_ms or 0))),
                "tokens": min(4_294_967_295, max(0, int(tokens or 0))),
            }
        ],
    )
    return ts


async def count_since(*, org: str, namespace: str, memory_key: str, since: str) -> tuple[int, int]:
    """(total, ok) of ledger rows newer than the `since` watermark."""
    from server.core.ch import get_client

    row = await get_client().query_one(
        f"""
        SELECT count() AS total, countIf(ok) AS ok
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key = {{key:String}} AND ts > {{since:DateTime64(3)}}
        """,
        {"org": org, "ns": namespace, "key": memory_key, "since": since or EPOCH},
    )
    if not row:
        return 0, 0
    return int(row.get("total") or 0), int(row.get("ok") or 0)


async def unfolded(
    *, org: str, namespace: str, memory_key: str, since: str, limit: int = 10_000
) -> list[dict[str, Any]]:
    """Runs newer than the watermark, oldest first, ready for merge_run."""
    from server.core.ch import get_client

    rows = await get_client().query_all(
        f"""
        SELECT path, ok, note, revision, agent, ts, steps_taken, duration_ms, tokens
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key = {{key:String}} AND ts > {{since:DateTime64(3)}}
        ORDER BY ts
        LIMIT {{lim:UInt32}}
        """,
        {"org": org, "ns": namespace, "key": memory_key, "since": since or EPOCH, "lim": limit},
    )
    return [
        {
            "path": list(r.get("path") or []),
            "ok": bool(r.get("ok")),
            "note": str(r.get("note") or ""),
            "revision": int(r.get("revision") or 0),
            "agent": str(r.get("agent") or ""),
            "steps": int(r.get("steps_taken") or 0),
            "duration_ms": int(r.get("duration_ms") or 0),
            "tokens": int(r.get("tokens") or 0),
            "ts": (
                format_ts(r["ts"]) if isinstance(r.get("ts"), datetime) else str(r.get("ts") or "")
            ),
        }
        for r in rows
    ]


async def failure_notes(
    *, org: str, namespace: str, memory_key: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Recent failed runs that left a note, attributed to the last transition
    the run took (the step it died on). This is the pitfalls text agents
    already wrote; locate serves it at the transition it belongs to."""
    from server.core.ch import get_client

    rows = await get_client().query_all(
        f"""
        SELECT
            if(length(path) >= 2, path[-2], '') AS src,
            if(length(path) >= 1, path[-1], '') AS dst,
            note, agent, ts
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key = {{key:String}} AND NOT ok AND note != ''
        ORDER BY ts DESC
        LIMIT {{lim:UInt32}}
        """,
        {"org": org, "ns": namespace, "key": memory_key, "lim": limit},
    )
    return [
        {
            "src": str(r.get("src") or ""),
            "dst": str(r.get("dst") or ""),
            "note": str(r.get("note") or "")[:512],
            "agent": str(r.get("agent") or ""),
            "ts": (
                format_ts(r["ts"]) if isinstance(r.get("ts"), datetime) else str(r.get("ts") or "")
            ),
        }
        for r in rows
    ]


async def stats_by_revision(
    *, org: str, namespace: str, memory_key: str
) -> dict[int, tuple[int, int]]:
    """{revision: (total, ok)} over the whole ledger for one procedure. The
    acceptance gate compares the revision under test with the one before."""
    from server.core.ch import get_client

    rows = await get_client().query_all(
        f"""
        SELECT revision, count() AS total, countIf(ok) AS ok
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND memory_key = {{key:String}}
        GROUP BY revision
        """,
        {"org": org, "ns": namespace, "key": memory_key},
    )
    return {
        int(r.get("revision") or 0): (int(r.get("total") or 0), int(r.get("ok") or 0)) for r in rows
    }


async def cost_by_revision(
    *, org: str, namespace: str, memory_key: str
) -> dict[int, dict[str, float]]:
    """{revision: {samples, p50_steps, p50_duration_ms, p50_tokens}} over the
    *successful* runs that reported any cost. The gate
    compares the revision under test with the one before on these."""
    from server.core.ch import get_client

    rows = await get_client().query_all(
        f"""
        SELECT revision,
               count() AS samples,
               quantileExact(0.5)(steps_taken) AS p50_steps,
               quantileExact(0.5)(duration_ms) AS p50_duration_ms,
               quantileExact(0.5)(tokens) AS p50_tokens
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND memory_key = {{key:String}}
          AND ok AND (steps_taken > 0 OR duration_ms > 0 OR tokens > 0)
        GROUP BY revision
        """,
        {"org": org, "ns": namespace, "key": memory_key},
    )
    return {
        int(r.get("revision") or 0): {
            "samples": float(r.get("samples") or 0),
            "p50_steps": float(r.get("p50_steps") or 0.0),
            "p50_duration_ms": float(r.get("p50_duration_ms") or 0.0),
            "p50_tokens": float(r.get("p50_tokens") or 0.0),
        }
        for r in rows
    }


__all__ = [
    "TABLE",
    "EPOCH",
    "format_ts",
    "record",
    "count_since",
    "unfolded",
    "stats_by_revision",
    "cost_by_revision",
    "failure_notes",
]
