"""loom.erasure_requests: the saga ledger the recovery worker, `erasure.status`
and the re-verify CLI read, and the checkpoint every step appends."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from server.core.ch import sequential_reads
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
        SELECT id, org, scope_type, scope_value, requester, actor, urgency, purge,
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
        SELECT id, org, scope_type, scope_value, requester, actor, urgency, purge,
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
        SELECT id, org, scope_type, scope_value, requester, actor, urgency, purge,
               attempts, steps_completed, requested_at, failure_reason,
               residue_count, residue, verified_at
        FROM loom.erasure_requests FINAL
        WHERE {where}
        ORDER BY requested_at
        """,
        params=params,
    )
    return cast("list[LedgerRow]", rows)


async def list_completed_requests(
    *,
    since: datetime,
    org: str | None = None,
    ch: _CHClient | None = None,
) -> list[LedgerRow]:
    """Sagas the ledger holds as `completed` whose census ran at or after `since`
    (naive UTC). After a rollout, `loom erasure-re-verify --completed-since` looks
    for the orphan row links they left: a build before N's tables completed without
    erasing them."""
    ch = ch or _default_client()
    where = "status = 'completed' AND verified_at >= {since:DateTime}"
    params: dict[str, Any] = {"since": since}
    if org:
        where += " AND org = {org:String}"
        params["org"] = org
    rows = await ch.query_all(
        f"""
        SELECT id, org, scope_type, scope_value, requester, actor, urgency, purge,
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


async def unfinished_since(
    org: str, since_s: int, margin_s: int, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The org's erasures that had not finished by `since_s` (ClickHouse clock, seconds):
    queued, in progress, failed, or completed after `since_s - margin_s`. Each row has
    `erasure_id`, `scope_type`, `scope_value` and `state`. Read with sequential
    consistency: a fence on it must see an erasure another replica just registered."""
    ch = ch or _default_client()
    with sequential_reads():
        return await ch.query_all(
            """
            SELECT toString(id) AS erasure_id, scope_type, scope_value, toString(status) AS state
            FROM loom.erasure_requests FINAL
            WHERE org = {org:String}
              AND (status != 'completed'
                   OR completed_at
                      >= fromUnixTimestamp({since:Int64}) - INTERVAL {margin:UInt32} SECOND)
            """,
            params={"org": org, "since": int(since_s), "margin": int(margin_s)},
        )
