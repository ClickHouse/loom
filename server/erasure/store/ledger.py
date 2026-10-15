"""loom.erasure_requests: the saga ledger the recovery worker, `erasure.status`
and the re-verify CLI read, and the checkpoint every step appends."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.rows import (
    CheckpointRow,
    LedgerRow,
    RequestStatusRow,
    ResumeRow,
)


async def list_stuck_in_progress(
    *,
    stale_threshold_seconds: int,
    max_attempts: int,
    batch_size: int,
    ch: _CHClient | None = None,
) -> list[ResumeRow]:
    """Find erasure sagas that need replaying — stuck `in_progress` (a crash
    mid-saga) OR `failed` (a step fail-closed on the last attempt) — past the
    heartbeat threshold and within the attempts budget. Every saga step is
    idempotent, so a `failed` saga re-runs only its incomplete steps next pass;
    one that exhausts the budget is filtered out here by `attempts < max_attempts`."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT id, org, scope_type, scope_value, requester, urgency,
               attempts, steps_completed, requested_at, failure_reason, residue
        FROM loom.erasure_requests FINAL
        WHERE status IN ('in_progress', 'failed')
          AND last_checkpoint_at < now() - INTERVAL {st:UInt32} SECOND
          AND attempts < {ma:UInt16}
        ORDER BY requested_at
        LIMIT {bs:UInt32}
        """,
        params={
            "st": int(stale_threshold_seconds),
            "ma": int(max_attempts),
            "bs": int(batch_size),
        },
    )
    return cast("list[ResumeRow]", rows)


# The failure_reason prefix of the recovery worker's terminal row: a saga at
# its attempts budget whose row does not start with it was never abandoned.
ABANDONED_REASON_PREFIX = "max_attempts ("


async def list_unabandoned_exhausted(
    *,
    stale_threshold_seconds: int,
    max_attempts: int,
    batch_size: int,
    ch: _CHClient | None = None,
) -> list[LedgerRow]:
    """Sagas that spent their attempts budget but whose abandonment never
    landed: still `in_progress` / `failed`, `attempts >= max_attempts`, and a
    failure_reason that is not the terminal row's. `list_stuck_in_progress`
    stops at the budget, so without this read such a saga would sit
    unresolved with no `erasure.abandoned` for an operator to act on."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT id, org, scope_type, scope_value, requester, urgency,
               attempts, steps_completed, requested_at, failure_reason,
               residue_count, residue, verified_at
        FROM loom.erasure_requests FINAL
        WHERE status IN ('in_progress', 'failed')
          AND last_checkpoint_at < now() - INTERVAL {st:UInt32} SECOND
          AND attempts >= {ma:UInt16}
          AND NOT startsWith(failure_reason, {pfx:String})
        ORDER BY requested_at
        LIMIT {bs:UInt32}
        """,
        params={
            "st": int(stale_threshold_seconds),
            "ma": int(max_attempts),
            "bs": int(batch_size),
            "pfx": ABANDONED_REASON_PREFIX,
        },
    )
    return cast("list[LedgerRow]", rows)


async def list_failed_requests(
    *,
    org: str | None = None,
    ch: _CHClient | None = None,
) -> list[LedgerRow]:
    """Every saga the ledger holds as `failed`, attempts budget spent or not,
    with its last reason and verify columns — what `loom erasure-re-verify`
    reads. The recovery worker stops at `max_attempts`; an
    operator does not."""
    ch = ch or _default_client()
    where = "status = 'failed'"
    params: dict[str, Any] = {}
    if org:
        where += " AND org = {org:String}"
        params["org"] = org
    rows = await ch.query_all(
        f"""
        SELECT id, org, scope_type, scope_value, requester, urgency,
               attempts, steps_completed, requested_at, failure_reason,
               residue_count, residue, verified_at
        FROM loom.erasure_requests FINAL
        WHERE {where}
        ORDER BY requested_at
        """,
        params=params,
    )
    return cast("list[LedgerRow]", rows)


async def fetch_request_status(
    erasure_id: str,
    *,
    ch: _CHClient | None = None,
) -> RequestStatusRow | None:
    """Fetch the latest state of one erasure request. FINAL collapses
    the per-step checkpoint rows so the caller sees the current status,
    current_step, failure_reason, etc."""
    ch = ch or _default_client()
    rows = await ch.query_one(
        """
        SELECT id, org, scope_type, scope_value, requester, status,
               current_step, failure_reason, requested_at, completed_at,
               residue_count, verified_at, residue
        FROM loom.erasure_requests FINAL
        WHERE id = {id:UUID}
        LIMIT 1
        """,
        params={"id": erasure_id},
    )
    return cast("RequestStatusRow | None", rows)


async def insert_request_checkpoint(
    row: CheckpointRow,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Append one row to loom.erasure_requests. Called after every Saga
    step + at terminal states; ReplacingMergeTree(version) collapses on
    `id` so the latest state always wins on FINAL reads."""
    ch = ch or _default_client()
    await ch.insert("loom.erasure_requests", [dict(row)])


async def list_requests(
    org: str,
    *,
    status: str | None,
    limit: int,
    scope_where: str,
    scope_params: dict[str, Any],
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The org's sagas newest first, each with its reversal count, narrowed by
    `status` and by `scope_where` (bare-column AND fragments over
    erasure_requests, bound by `scope_params`)."""
    ch = ch or _default_client()
    where = "org = {org:String}" + (" AND status = {st:String}" if status else "") + scope_where
    params: dict[str, Any] = {**scope_params, "org": org, "lim": int(limit)}
    if status:
        params["st"] = status
    # CH 26.5's analyzer mishandles a WHERE on a FINAL table that is also a JOIN
    # side (it evaluates the whole AND predicate as a column against an empty
    # FINAL block → NOT_FOUND_COLUMN_IN_BLOCK). Filter + FINAL in an isolated
    # subquery, THEN LEFT JOIN the reversal counts.
    return await ch.query_all(
        f"""
        WITH reversals AS (
            SELECT erasure_id, count() AS n
            FROM loom.erasure_reversals FINAL
            WHERE org = {{org:String}}
            GROUP BY erasure_id
        )
        SELECT e.id AS id, e.scope_type AS scope_type,
               e.scope_value AS scope_value, e.requester AS requester,
               e.status AS status, e.requested_at AS requested_at,
               e.completed_at AS completed_at,
               e.failure_reason AS failure_reason,
               length(e.steps_completed) AS steps_completed_count,
               coalesce(r.n, 0) AS reversals_count,
               e.residue_count AS residue_count, e.verified_at AS verified_at
        FROM (
            SELECT id, scope_type, scope_value, requester, status,
                   requested_at, completed_at, failure_reason, steps_completed,
                   residue_count, verified_at
            FROM loom.erasure_requests FINAL
            WHERE {where}
            ORDER BY requested_at DESC
            LIMIT {{lim:UInt32}}
        ) AS e
        LEFT JOIN reversals AS r ON e.id = r.erasure_id
        ORDER BY e.requested_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


# The memory journal's erasure fence: a write, or a current-row repair, must not
# bring back rows an erasure of its namespace or users is removing.
# The erasure requests of a scope, and the ones that hold it: running, or failed and
# still being retried (its content may be half deleted). An abandoned one is
# escalated to an operator and no longer holds anything.
_SCOPE = """
    org = {org:String}
    AND ((scope_type = 'namespace' AND scope_value = {ns:String})
      OR (scope_type = 'namespace_list' AND position(scope_value, {quoted:String}) > 0)
      OR (scope_type = 'user_id' AND scope_value IN {users:Array(String)}))"""
_HOLDS = (
    "(status = 'in_progress' "
    "OR (status = 'failed' AND NOT startsWith(failure_reason, {abandoned:String})))"
)


def _scope_params(org: str, namespace: str, user_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "org": org,
        "ns": namespace,
        "quoted": f'"{namespace}"',
        "users": [u for u in user_ids if u and u != "-"],
        "abandoned": ABANDONED_REASON_PREFIX,
    }


async def scope_erasures(
    org: str,
    namespace: str,
    user_ids: Sequence[str],
    since: datetime,
    *,
    ch: _CHClient | None = None,
) -> tuple[int, int]:
    """(erasure requests ever touching this namespace or these users, those
    requested after `since` or holding the scope)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT count() AS n, countIf(requested_at >= {{since:DateTime}} OR {_HOLDS}) AS late "
        f"FROM loom.erasure_requests FINAL WHERE {_SCOPE}",
        params={**_scope_params(org, namespace, user_ids), "since": since.replace(tzinfo=None)},
    )
    return int((row or {}).get("n") or 0), int((row or {}).get("late") or 0)


async def scope_held(
    org: str, namespace: str, user_ids: Sequence[str], *, ch: _CHClient | None = None
) -> bool:
    """Does an erasure hold this namespace or these users?"""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT count() AS n FROM loom.erasure_requests FINAL WHERE {_SCOPE} AND {_HOLDS}",
        params=_scope_params(org, namespace, user_ids),
    )
    return int((row or {}).get("n") or 0) > 0


async def scope_erasure_times(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> tuple[int, list[tuple[str, int]]]:
    """When erasures of this namespace, and of each erased user of the org, were
    last requested (epoch µs, whatever their status): what was recorded before is
    gone, so a read must never serve it. `requested_at` holds whole seconds; the
    time is rounded up, so the second of the request counts as before it. Read
    from the ledger alone: which users have content can change while a read runs.
    ponytail: every erased user of the org rides each read's cut; past some
    thousands, join the ledger inside the content query instead."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT scope_type, scope_value, (toUnixTimestamp(max(requested_at)) + 1) * 1000000 AS at
        FROM loom.erasure_requests FINAL
        WHERE org = {org:String}
          AND ((scope_type = 'namespace' AND scope_value = {ns:String})
            OR (scope_type = 'namespace_list' AND position(scope_value, {quoted:String}) > 0)
            OR scope_type = 'user_id')
        GROUP BY scope_type, scope_value
        """,
        params={"org": org, "ns": namespace, "quoted": f'"{namespace}"'},
    )
    ns_at = max((int(r["at"]) for r in rows if r["scope_type"] != "user_id"), default=0)
    users = sorted(
        (str(r["scope_value"]), int(r["at"])) for r in rows if r["scope_type"] == "user_id"
    )
    return ns_at, users
