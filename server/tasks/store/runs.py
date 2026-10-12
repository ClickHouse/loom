"""SQL for the run ledger: `loom.agent_runs` and the reads a run derives from
the planes that carry its `run_id` / `session_id` (`loom.memory`,
`loom.search_trace_log`, `loom.agent_turn_usage`).

`loom.agent_runs` is a ReplacingMergeTree on (org, namespace, run_id): every
write is a whole version row (`run_row` fills the columns a partial row
lacks), and a read is `FINAL`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from server.core.ch import sequential_reads
from server.core.schema.migrate_lease import key_exists
from server.identity.domain.read_scope import ReadFilters, scope_and
from server.tasks.store.rows import RunLedgerRow

log = logging.getLogger(__name__)

# Cap on the keys a derived read returns.
DERIVED_LIMIT = 500

RUN_COLUMNS = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "run_id",
    "goal",
    "user_request",
    "expected_outcome",
    "environment",
    "model",
    "consumer",
    "status",
    "started_at",
    "completed_at",
    "duration_ms",
    "final_response",
    "confidence",
    "memories_used",
    "memories_written",
    "metadata",
    # The run's graded outcome, set by run.grade after close. Every write of the row
    # carries them, or a later run.complete would clear a grade that already landed
    # (ReplacingMergeTree(version)).
    "grade",
    "grade_source",
    "grade_reason",
    "grade_ref",
    "graded_at",
)
_RUN_SELECT = ", ".join(RUN_COLUMNS)
# A row read without a column (a replica behind the grade migration, a fake) still writes
# it: the grade columns' own defaults, never a KeyError on close.
_COLUMN_DEFAULTS: dict[str, Any] = {
    "grade": "",
    "grade_source": "",
    "grade_reason": "",
    "grade_ref": "",
    "graded_at": None,
}
_SYNC_INSERT = {"async_insert": 0, "wait_for_async_insert": 1}


def run_row(row: RunLedgerRow) -> dict[str, Any]:
    """Every agent_runs column of `row`, the grade columns defaulted."""
    cols: dict[str, Any] = dict(row)
    return {c: cols[c] if c in cols else _COLUMN_DEFAULTS[c] for c in RUN_COLUMNS}


async def fetch_run(ch: Any, org: str, ns: str, run_id: str) -> RunLedgerRow | None:
    row: RunLedgerRow | None = await ch.query_one(
        f"""
        SELECT {_RUN_SELECT}
        FROM loom.agent_runs FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND run_id = {{rid:String}}
        LIMIT 1
        """,
        params={"org": org, "ns": ns, "rid": run_id},
    )
    return row


async def insert_runs(ch: Any, rows: list[RunLedgerRow]) -> None:
    await ch.insert("loom.agent_runs", [run_row(r) for r in rows], settings=_SYNC_INSERT)


# ---------------------------------------------------------------- run id claims

# Whether loom.agent_run_claims exists here (its migration ran on a server with a
# Keeper): None until the first run.begin of this process asks.
_claims_atomic: bool | None = None


@dataclass(frozen=True)
class RunClaim:
    """Who holds the claim on a run id. `won`: this call's insert made it."""

    won: bool
    user_id: str
    session_id: str


def claims_atomic() -> bool | None:
    """Whether run ids are claimed in Keeper here; None = not asked yet."""
    return _claims_atomic


async def _claims_available(ch: Any) -> bool:
    # The same detection as the task lease backend (`lease_backend`).
    global _claims_atomic
    if _claims_atomic is None:
        row = await ch.query_one(
            "SELECT engine FROM system.tables WHERE database = 'loom' AND name = 'agent_run_claims'"
        )
        _claims_atomic = bool(row) and row.get("engine") == "KeeperMap"
        if not _claims_atomic:
            log.info("run.begin: no loom.agent_run_claims (no Keeper); run ids are not claimed")
    return _claims_atomic


async def claim_run(
    ch: Any, org: str, ns: str, run_id: str, user_id: str, session_id: str
) -> RunClaim | None:
    """Claim (org, ns, run_id) for this user and session, or read the claim that
    already holds it. None where the server has no Keeper."""
    if not await _claims_available(ch):
        return None
    key = json.dumps([org, ns, run_id])
    try:
        await ch.execute(
            "INSERT INTO loom.agent_run_claims "
            "(claim_key, org, namespace, run_id, user_id, session_id, claimed_at) "
            "SETTINGS keeper_map_strict_mode = 1 "
            "VALUES ({k:String}, {org:String}, {ns:String}, {rid:String}, "
            "{uid:String}, {sid:String}, now64(3))",
            params={
                "k": key,
                "org": org,
                "ns": ns,
                "rid": run_id,
                "uid": user_id,
                "sid": session_id,
            },
        )
        return RunClaim(won=True, user_id=user_id, session_id=session_id)
    except Exception as e:
        if not key_exists(e):
            raise
    row = await ch.query_one(
        "SELECT user_id, session_id FROM loom.agent_run_claims WHERE claim_key = {k:String}",
        params={"k": key},
    )
    if row is None:  # the key exists but cannot be read: never the caller's
        return RunClaim(won=False, user_id="", session_id="")
    return RunClaim(won=False, user_id=str(row["user_id"]), session_id=str(row["session_id"]))


async def delete_claim(ch: Any, org: str, ns: str, run_id: str) -> None:
    """Drop the run id's claim once its run row is closed: from then on the row
    itself keeps other users off the id. A Keeper key drop, not a part mutation."""
    if not await _claims_available(ch):
        return
    await ch.execute(
        "ALTER TABLE loom.agent_run_claims DELETE WHERE claim_key = {k:String}",
        params={"k": json.dumps([org, ns, run_id])},
    )


async def runs_to_grade(
    ch: Any, org: str, ns: str, *, run_id: str, session_id: str, limit: int
) -> list[RunLedgerRow]:
    """The run `run_id` names, else the session's runs, newest first — read
    with sequential consistency (the grade writes from them)."""
    target = "run_id = {rid:String}" if run_id else "session_id = {sid:String}"
    with sequential_reads():
        rows: list[RunLedgerRow] = await ch.query_all(
            f"""
            SELECT {_RUN_SELECT}
            FROM loom.agent_runs FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND {target}
            ORDER BY started_at DESC
            LIMIT {{lim:UInt32}}
            """,
            params={"org": org, "ns": ns, "rid": run_id, "sid": session_id, "lim": limit},
        )
    return rows


async def list_runs(
    ch: Any, org: str, ns: str, *, where: list[str], params: dict[str, Any], limit: int
) -> list[RunLedgerRow]:
    """The namespace's runs matching the extra `where` fragments, newest first."""
    clauses = ["org = {org:String}", "namespace = {ns:String}", *where]
    rows: list[RunLedgerRow] = await ch.query_all(
        f"""
        SELECT {_RUN_SELECT}
        FROM loom.agent_runs FINAL
        WHERE {" AND ".join(clauses)}
        ORDER BY started_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params={**params, "org": org, "ns": ns, "lim": limit},
    )
    return rows


async def recent_run_heads(
    ch: Any, org: str, ns: str, *, where: list[str], params: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    """Goal, status and timing of the namespace's newest runs matching the extra
    `where` fragments, newest first."""
    clauses = ["org = {org:String}", "namespace = {ns:String}", *where]
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT run_id, session_id, goal, toString(status) AS status,
               started_at, duration_ms
        FROM loom.agent_runs FINAL
        WHERE {" AND ".join(clauses)}
        ORDER BY started_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params={**params, "org": org, "ns": ns, "lim": limit},
    )
    return rows


# ---------------------------------------------------------------- derivations


async def written_keys(ch: Any, org: str, ns: str, run_id: str) -> list[str]:
    """memory_keys written under this run_id. Episodic observations are
    counted separately and the run's own reflection is reported as
    `reflection_key`: neither is a fact the run produced."""
    rows = await ch.query_all(
        """
        SELECT DISTINCT memory_key
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND run_id = {rid:String}
          AND memory_type NOT IN ('episodic', 'reflection')
          AND tombstoned_at IS NULL
        ORDER BY memory_key
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": ns, "rid": run_id, "lim": DERIVED_LIMIT},
    )
    return [str(r["memory_key"]) for r in rows if r.get("memory_key")]


async def observation_count(ch: Any, org: str, ns: str, run_id: str) -> int:
    row = await ch.query_one(
        """
        SELECT count() AS n FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND run_id = {rid:String} AND memory_type = 'episodic'
          AND tombstoned_at IS NULL
        """,
        params={"org": org, "ns": ns, "rid": run_id},
    )
    return int((row or {}).get("n") or 0)


async def traced_recall_keys(ch: Any, org: str, ns: str, run_id: str) -> list[str]:
    """Distinct keys memory.search returned under the run, from the trace log."""
    rows = await ch.query_all(
        """
        SELECT DISTINCT arrayJoin(returned_keys) AS memory_key
        FROM loom.search_trace_log
        WHERE org = {org:String} AND namespace = {ns:String} AND run_id = {rid:String}
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": ns, "rid": run_id, "lim": DERIVED_LIMIT},
    )
    return [str(r["memory_key"]) for r in rows if r.get("memory_key")]


async def session_cost(ch: Any, org: str, ns: str, session_id: str) -> tuple[int, float]:
    """(turns, cost in USD) the session's usage rows record."""
    row = await ch.query_one(
        """
        SELECT count() AS turns, sum(cost_usd) AS cost
        FROM loom.agent_turn_usage FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND session_id = {sid:String}
        """,
        params={"org": org, "ns": ns, "sid": session_id},
    )
    if not row:
        return 0, 0.0
    return int(row.get("turns") or 0), round(float(row.get("cost") or 0.0), 6)


async def live_memories_by_key(
    ch: Any, org: str, ns: str, keys: list[str], *, scope: ReadFilters
) -> list[dict[str, Any]]:
    """One row per live memory_key of `keys` inside `scope`: its newest memory_id
    (as `memory_id`), memory_type, agent, user_id and session_id. A key is shared
    by every session's copy of the fact, and a run's credit is one use."""
    params: dict[str, Any] = {"org": org, "ns": ns, "keys": keys}
    in_scope = scope_and(scope, params)
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT argMax(toString(memory_id), updated_at) AS mid, memory_key,
               any(memory_type) AS memory_type, any(agent) AS agent,
               any(user_id) AS user_id, any(session_id) AS session_id
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key IN {{keys:Array(String)}} AND tombstoned_at IS NULL{in_scope}
        GROUP BY memory_key
        """,
        params=params,
    )
    # `AS mid`, not `AS memory_id`: a same-named alias shadows the column in WHERE.
    for r in rows:
        r["memory_id"] = str(r.get("mid") or r.get("memory_id") or "")
    return rows


__all__ = [
    "DERIVED_LIMIT",
    "RUN_COLUMNS",
    "fetch_run",
    "insert_runs",
    "list_runs",
    "live_memories_by_key",
    "observation_count",
    "run_row",
    "runs_to_grade",
    "session_cost",
    "traced_recall_keys",
    "written_keys",
]
