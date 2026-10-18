"""SQL for the run ledger: `loom.agent_runs` and the reads a run derives from
the planes that carry its `run_id` / `session_id` (`loom.memory`,
`loom.search_trace_log`, `loom.agent_turn_usage`).

`loom.agent_runs` is a ReplacingMergeTree on (org, namespace, run_id): every
write is a whole version row (`run_row` fills the columns a partial row
lacks), and a read is `FINAL`.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.ch import sequential_reads
from server.core.leases import key_exists
from server.identity.domain.read_scope import ReadFilters, scope_and
from server.observability import degradations, provider_health
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
    # The run's optional budget (0 = none), set by run.begin and carried by every
    # later write of the row like the grade columns.
    "budget_usd",
    "budget_seconds",
)
_RUN_SELECT = ", ".join(RUN_COLUMNS)
# A row read without a column (a replica behind the grade or budget migration, a fake)
# still writes it: the columns' own defaults, never a KeyError on close.
_COLUMN_DEFAULTS: dict[str, Any] = {
    "grade": "",
    "grade_source": "",
    "grade_reason": "",
    "grade_ref": "",
    "graded_at": None,
    "budget_usd": 0.0,
    "budget_seconds": 0,
}
_SYNC_INSERT = {"async_insert": 0, "wait_for_async_insert": 1}


def run_row(row: RunLedgerRow) -> dict[str, Any]:
    """Every agent_runs column of `row`, the grade and budget columns defaulted."""
    cols: dict[str, Any] = dict(row)
    out = {c: cols[c] if c in cols else _COLUMN_DEFAULTS[c] for c in RUN_COLUMNS}
    if "version" in cols:
        out["version"] = cols["version"]
    return out


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

# A Keeper error is logged once per kind of failure, then only noted.
_keeper_errors_logged: set[str] = set()


@dataclass(frozen=True)
class RunClaim:
    """Who holds the claim on a run id. `won`: this call's insert made it.
    `unreadable`: the key is held but its holder could not be read."""

    won: bool
    user_id: str
    session_id: str
    unreadable: bool = False


class NoClaim(enum.Enum):
    """Why claim_run holds no claim. UNAVAILABLE: Keeper failed, so nothing keeps
    two users' first begins apart. RELEASED: the key was
    taken and dropped again, so its holder's row is already written."""

    UNAVAILABLE = "unavailable"
    RELEASED = "released"


def _keeper_failed(e: BaseException, what: str = "Keeper claim failed") -> None:
    """The claim could not be made: run.begin falls back to the row check (a
    narrowed caller only re-opens its own run, else gets a fresh id)."""
    if what not in _keeper_errors_logged:
        _keeper_errors_logged.add(what)
        log.warning("run.begin: %s, checking the run row only: %s", what, e)
    with provider_health.unrecorded():
        degradations.note("run_claims", "row_check_only", e)


async def claim_run(
    ch: Any, org: str, ns: str, run_id: str, user_id: str, session_id: str
) -> RunClaim | NoClaim:
    """Claim (org, ns, run_id) for this user and session, or read the claim that
    already holds it. NoClaim says why there is none."""
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
    except Exception as e:  # noqa: BLE001 — Keeper down falls back to the row check
        if not key_exists(e):
            _keeper_failed(e)
            return NoClaim.UNAVAILABLE
    return await _claim_holder(ch, key)


async def _claim_holder(ch: Any, key: str) -> RunClaim | NoClaim:
    """Who holds `key`, after our insert found it taken. RELEASED when the key is gone
    again (its holder wrote its row and released it): the row check then decides.
    A key that cannot be read is never the caller's."""
    try:
        row = await ch.query_one(
            "SELECT user_id, session_id FROM loom.agent_run_claims WHERE claim_key = {k:String}",
            params={"k": key},
        )
    except Exception as e:  # noqa: BLE001 — unreadable counts as another user's claim
        log.debug("run.begin: claim %s not readable: %s", key, e)
        with provider_health.unrecorded():
            degradations.note("run_claims", "claim_unreadable", e)
        return RunClaim(won=False, user_id="", session_id="", unreadable=True)
    if row is None:
        return NoClaim.RELEASED
    return RunClaim(won=False, user_id=str(row["user_id"]), session_id=str(row["session_id"]))


async def delete_claim(ch: Any, org: str, ns: str, run_id: str) -> None:
    """Drop the run id's claim once its run row is written: from then on the row
    itself keeps other users off the id. A Keeper key drop, not a part mutation.
    Best effort: a claim left behind only sends other users to a fresh id."""
    try:
        await ch.execute(
            "ALTER TABLE loom.agent_run_claims DELETE WHERE claim_key = {k:String}",
            params={"k": json.dumps([org, ns, run_id])},
        )
    except Exception as e:  # noqa: BLE001 — a claim left behind never fails the caller
        log.debug("run claim for %s not dropped: %s", run_id, e)


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


# run.list's orders. Each ends on started_at and run_id (unique in a namespace under
# FINAL), so an OFFSET page boundary never repeats or skips a run.
LIST_ORDERS: dict[str, str] = {
    "started_desc": "started_at DESC, run_id DESC",
    "started_asc": "started_at ASC, run_id ASC",
    "duration_desc": "duration_ms DESC, started_at DESC, run_id DESC",
    "duration_asc": "duration_ms ASC, started_at ASC, run_id ASC",
    "relied_on_desc": "length(memories_used) DESC, started_at DESC, run_id DESC",
    "relied_on_asc": "length(memories_used) ASC, started_at ASC, run_id ASC",
}


async def list_runs(
    ch: Any,
    org: str,
    ns: str,
    *,
    where: list[str],
    params: dict[str, Any],
    limit: int,
    offset: int = 0,
    order_by: str = "started_desc",
) -> list[RunLedgerRow]:
    """One page of the namespace's runs matching the extra `where` fragments, in
    `order_by` order (a key of LIST_ORDERS), `offset` runs in."""
    # The order is written into the statement: only a LIST_ORDERS key gets that far.
    order = LIST_ORDERS[order_by]
    clauses = ["org = {org:String}", "namespace = {ns:String}", *where]
    rows: list[RunLedgerRow] = await ch.query_all(
        f"""
        SELECT {_RUN_SELECT}
        FROM loom.agent_runs FINAL
        WHERE {" AND ".join(clauses)}
        ORDER BY {order}
        LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}
        """,
        params={**params, "org": org, "ns": ns, "lim": limit, "off": offset},
    )
    return rows


async def count_runs(
    ch: Any, org: str, ns: str, *, where: list[str], params: dict[str, Any]
) -> int:
    """How many of the namespace's runs match the extra `where` fragments: the
    rows `list_runs` pages through."""
    clauses = ["org = {org:String}", "namespace = {ns:String}", *where]
    row = await ch.query_one(
        f"""
        SELECT count() AS n
        FROM loom.agent_runs FINAL
        WHERE {" AND ".join(clauses)}
        """,
        params={**params, "org": org, "ns": ns},
    )
    return int((row or {}).get("n") or 0)


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


async def written_keys(ch: Any, org: str, ns: str, run_id: str, *, scope: ReadFilters) -> list[str]:
    """memory_keys written under this run_id inside `scope`. Episodic observations
    are counted separately and the run's own reflection is reported as
    `reflection_key`: neither is a fact the run produced."""
    params: dict[str, Any] = {"org": org, "ns": ns, "rid": run_id, "lim": DERIVED_LIMIT}
    in_scope = scope_and(scope, params)
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT memory_key
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND run_id = {{rid:String}}
          AND memory_type NOT IN ('episodic', 'reflection')
          AND tombstoned_at IS NULL{in_scope}
        ORDER BY memory_key
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return [str(r["memory_key"]) for r in rows if r.get("memory_key")]


async def observation_count(ch: Any, org: str, ns: str, run_id: str, *, scope: ReadFilters) -> int:
    params: dict[str, Any] = {"org": org, "ns": ns, "rid": run_id}
    in_scope = scope_and(scope, params)
    row = await ch.query_one(
        f"""
        SELECT count() AS n FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND run_id = {{rid:String}} AND memory_type = 'episodic'
          AND tombstoned_at IS NULL{in_scope}
        """,
        params=params,
    )
    return int((row or {}).get("n") or 0)


async def active_runs_by_id(ch: Any, org: str, run_id: str) -> list[RunLedgerRow]:
    """The active MCP session runs under `run_id` in `org`, oldest first: one, or
    one per namespace when the subject's tokens default to different ones."""
    rows: list[RunLedgerRow] = await ch.query_all(
        f"""
        SELECT {_RUN_SELECT}
        FROM loom.agent_runs FINAL
        WHERE org = {{org:String}} AND run_id = {{rid:String}} AND status = 'active'
          AND environment = 'mcp' AND match(run_id, '^mcp-[0-9a-f]{{40}}$')
        ORDER BY started_at, namespace
        """,
        params={"org": org, "rid": run_id},
    )
    return rows


async def idle_session_runs(ch: Any, *, idle_seconds: int, limit: int) -> list[RunLedgerRow]:
    """Up to `limit` active implicit MCP session runs, in every org, that
    no request of their token reached within `idle_seconds`, in random order so
    runs that fail to close cannot hold the front."""
    rows: list[RunLedgerRow] = await ch.query_all(
        """
        WITH candidates AS (
            SELECT run_id FROM loom.agent_runs
            WHERE status = 'active' AND environment = 'mcp'
              -- Only the transport writes this shape (run.begin remaps it).
              AND match(run_id, '^mcp-[0-9a-f]{40}$')
        ),
        -- Calls by run and token; a run that records no subject (no auth) stays
        -- busy while any call reaches it.
        recent AS (
            SELECT org, run_id, subject FROM loom.agent_events
            WHERE ts >= now64(3) - INTERVAL {idle:UInt32} SECOND AND run_id IN candidates
        )
        SELECT org, namespace, run_id FROM loom.agent_runs FINAL
        WHERE run_id IN candidates
          AND status = 'active' AND JSONExtractBool(metadata, 'implicit')
          -- Neither begun nor written since: a first begin's version is no time
          -- (begin_session_run), so its start says when it was begun.
          AND started_at < now64(3) - INTERVAL {idle:UInt32} SECOND
          AND version < toUInt64(toUnixTimestamp64Nano(now64(9) - INTERVAL {idle:UInt32} SECOND))
          AND (org, run_id, JSONExtractString(metadata, 'subject')) NOT IN recent
          AND (
              JSONExtractString(metadata, 'subject') != ''
              OR (org, run_id) NOT IN (SELECT org, run_id FROM recent)
          )
        ORDER BY rand()
        LIMIT {lim:UInt32}
        """,
        params={"idle": idle_seconds, "lim": limit},
    )
    return rows


async def busy_lately(
    ch: Any, org: str, ns: str, run_id: str, subject: str, idle_seconds: int
) -> bool:
    """Whether the run was begun or re-opened, or a request of the token
    `subject` ("" = anyone) labelled it, within `idle_seconds`."""
    row = await ch.query_one(
        """
        SELECT
            (SELECT count() FROM loom.agent_events
             WHERE org = {org:String} AND run_id = {rid:String}
               AND ts >= now64(3) - INTERVAL {idle:UInt32} SECOND
               AND ({subject:String} = '' OR subject = {subject:String})) > 0
            OR (SELECT max(version) FROM loom.agent_runs
                WHERE org = {org:String} AND namespace = {ns:String} AND run_id = {rid:String})
               >= toUInt64(toUnixTimestamp64Nano(now64(9) - INTERVAL {idle:UInt32} SECOND))
            OR (SELECT max(started_at) FROM loom.agent_runs
                WHERE org = {org:String} AND namespace = {ns:String} AND run_id = {rid:String})
               >= now64(3) - INTERVAL {idle:UInt32} SECOND
            AS busy
        """,
        params={"org": org, "ns": ns, "rid": run_id, "subject": subject, "idle": idle_seconds},
    )
    return bool((row or {}).get("busy"))


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


async def run_cost(
    ch: Any, org: str, ns: str, session_id: str, since: datetime, until: datetime
) -> tuple[int, int, float]:
    """(turns, unpriced turns, cost in USD) of the session's turns with
    since <= turn_at <= until. An unpriced turn costs 0."""
    row = await ch.query_one(
        """
        SELECT count() AS turns, countIf(NOT priced) AS unpriced, sum(cost_usd) AS cost
        FROM loom.agent_turn_usage FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND session_id = {sid:String}
          AND turn_at >= {since:DateTime64(3)} AND turn_at <= {until:DateTime64(3)}
        """,
        params={"org": org, "ns": ns, "sid": session_id, "since": since, "until": until},
    )
    if not row:
        return 0, 0, 0.0
    turns, unpriced = int(row.get("turns") or 0), int(row.get("unpriced") or 0)
    return turns, unpriced, round(float(row.get("cost") or 0.0), 6)


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
    "run_cost",
    "run_row",
    "runs_to_grade",
    "traced_recall_keys",
    "written_keys",
]
