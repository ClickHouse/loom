"""loom.erasure_requests: the saga ledger the recovery worker, `erasure.status`
and the re-verify CLI read, and the checkpoint every step appends."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from server.core.ch import sequential_reads
from server.core.naming import mirror_name_sql
from server.core.repo_base import _CHClient, _default_client
from server.erasure.domain.failure_reason import ABANDONED_REASON_PREFIX, REPLAY_PENDING_REASON
from server.erasure.store.rows import (
    CheckpointRow,
    LedgerRow,
    RequestStatusRow,
    ResumeRow,
)

# The ledger rows a read may act on: the table's TTL drops a saga's rows part by part 90
# days after `requested_at`, which every checkpoint of the saga repeats, so near the end an
# older checkpoint can outlive the terminal row and read as the saga's state. The fences
# (`unfinished_since`, `unfinished_reaching`) do not apply it: a stale unfinished row keeps
# fencing, which fails closed. `open_namespace_erasures` does, as the recovery worker's
# `still_open` does: a saga that will never resume again must not refuse source adds.
LIVE_LEDGER = "requested_at > now() - INTERVAL 89 DAY"

# A saga that will still run: queued, in progress, or failed and awaiting the recovery
# worker (not abandoned, not waiting for a replay). Takes `abandoned` and `pending`.
WILL_RUN = (
    "(status IN ('queued', 'in_progress') OR (status = 'failed' "
    "AND NOT startsWith(failure_reason, {abandoned:String}) "
    "AND failure_reason != {pending:String}))"
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
        WHERE status IN ('in_progress', 'failed') AND """
        + LIVE_LEDGER
        + """
          AND last_checkpoint_at < now() - INTERVAL {st:UInt32} SECOND
          AND attempts < {ma:UInt16}
          AND failure_reason != {pending:String}
        ORDER BY requested_at
        LIMIT {bs:UInt32}
        """,
        params={
            "st": int(stale_threshold_seconds),
            "ma": int(max_attempts),
            "bs": int(batch_size),
            "pending": REPLAY_PENDING_REASON,
        },
    )
    return cast("list[ResumeRow]", rows)


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
               residue_count, residue, verified_at, census
        FROM loom.erasure_requests FINAL
        WHERE status IN ('in_progress', 'failed') AND """
        + LIVE_LEDGER
        + """
          AND last_checkpoint_at < now() - INTERVAL {st:UInt32} SECOND
          AND attempts >= {ma:UInt16}
          AND NOT startsWith(failure_reason, {pfx:String})
          AND failure_reason != {pending:String}
        ORDER BY requested_at
        LIMIT {bs:UInt32}
        """,
        params={
            "st": int(stale_threshold_seconds),
            "ma": int(max_attempts),
            "bs": int(batch_size),
            "pfx": ABANDONED_REASON_PREFIX,
            "pending": REPLAY_PENDING_REASON,
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
    where = "status = 'failed' AND " + LIVE_LEDGER
    params: dict[str, Any] = {}
    if org:
        where += " AND org = {org:String}"
        params["org"] = org
    rows = await ch.query_all(
        f"""
        SELECT id, org, scope_type, scope_value, requester, actor, urgency, purge,
               attempts, steps_completed, requested_at, failure_reason,
               residue_count, residue, verified_at, census
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
    where = "status = 'completed' AND verified_at >= {since:DateTime} AND " + LIVE_LEDGER
    params: dict[str, Any] = {"since": since}
    if org:
        where += " AND org = {org:String}"
        params["org"] = org
    rows = await ch.query_all(
        f"""
        SELECT id, org, scope_type, scope_value, requester, actor, urgency, purge,
               attempts, steps_completed, requested_at, failure_reason,
               residue_count, residue, verified_at, census
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
               residue_count, verified_at, residue, census, purge
        FROM loom.erasure_requests FINAL
        WHERE id = {id:UUID} AND """
        + LIVE_LEDGER
        + """
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


# The ledger holds a list scope as `{"namespaces": [...], ...}`. `only_mirrors` also reads
# the bare-array form, which the ledger never holds: `begin_erasure` stores every list as
# an object.
_LIST_NAMESPACES = "JSONExtract(scope_value, 'namespaces', 'Array(String)')"
# Leaves out the sagas whose scope names mirrors only (`scopes.only_mirrors`), which only a
# platform admin's list shows.
NOT_ONLY_MIRRORS = (
    f" AND NOT (scope_type = 'namespace' AND {mirror_name_sql('scope_value')})"
    f" AND NOT (scope_type = 'namespace_list' AND notEmpty({_LIST_NAMESPACES})"
    f" AND arrayAll(x -> {mirror_name_sql('x')}, {_LIST_NAMESPACES}))"
)


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
    where = (
        "org = {org:String} AND "
        + LIVE_LEDGER
        + (" AND status = {st:String}" if status else "")
        + scope_where
    )
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


# An erasure still open for a reader that started at `since`: not completed, or completed
# after it, less the clock margin.
_UNFINISHED_SINCE = """(status != 'completed'
    OR completed_at >= fromUnixTimestamp({since:Int64}) - INTERVAL {margin:UInt32} SECOND)"""


async def unfinished_since(
    org: str,
    since_s: int,
    margin_s: int,
    *,
    limit: int | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The org's erasures that had not finished by `since_s` (ClickHouse clock, seconds):
    in progress, failed, or completed after `since_s - margin_s`, at most `limit` of
    them. Each row has `erasure_id`, `scope_type`, `scope_value`, `state` and
    `completed_at`. Read with
    sequential consistency: a fence on it must see an erasure another replica just
    registered."""
    ch = ch or _default_client()
    limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
    with sequential_reads():
        return await ch.query_all(
            f"""
            SELECT toString(id) AS erasure_id, scope_type, scope_value,
                   toString(status) AS state, completed_at
            FROM loom.erasure_requests FINAL
            WHERE org = {{org:String}} AND {_UNFINISHED_SINCE}
            {limit_clause}
            """,
            params={"org": org, "since": int(since_s), "margin": int(margin_s)},
        )


async def unfinished_reaching(
    org: str,
    namespace: str,
    user_id: str | None,
    since_s: int,
    margin_s: int,
    *,
    skip_abandoned: bool = False,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The erasures unfinished since `since_s` that may reach a row of `namespace`: its own,
    the user scopes of `user_id` (of every user when None), and every other scope, which
    the caller parses (namespace lists). `skip_abandoned` leaves out a saga the recovery
    worker gave up on; resumed, it is `in_progress` again. Each row has `scope_type`,
    `scope_value` and `purge`."""
    ch = ch or _default_client()
    with sequential_reads():
        return await ch.query_all(
            f"""
            SELECT scope_type, scope_value, purge
            FROM loom.erasure_requests FINAL
            WHERE org = {{org:String}} AND {_UNFINISHED_SINCE}
              AND NOT ({{skip_abandoned:Bool}} AND status = 'failed'
                       AND startsWith(failure_reason, {{abandoned:String}}))
              AND ((scope_type = 'namespace' AND scope_value = {{ns:String}})
                   OR (scope_type = 'user_id'
                       AND ({{any_user:Bool}} OR scope_value = {{uid:String}}))
                   OR scope_type NOT IN ('namespace', 'user_id'))
            """,
            params={
                "org": org,
                "ns": namespace,
                "uid": user_id or "",
                "any_user": user_id is None,
                "since": int(since_s),
                "margin": int(margin_s),
                "skip_abandoned": skip_abandoned,
                "abandoned": ABANDONED_REASON_PREFIX,
            },
        )


async def open_namespace_erasures(org: str, *, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """The org's namespace and namespace-list erasures that will still run: queued, in
    progress, or failed and awaiting the recovery worker (not abandoned, not waiting for a
    replay). Each row has `scope_type` and `scope_value`. Read with sequential consistency:
    a fence on it must see an erasure another replica just registered."""
    ch = ch or _default_client()
    with sequential_reads():
        return await ch.query_all(
            "SELECT scope_type, scope_value FROM loom.erasure_requests FINAL "
            "WHERE org = {org:String} AND " + LIVE_LEDGER + " "
            "AND scope_type IN ('namespace', 'namespace_list') AND " + WILL_RUN,
            params={
                "org": org,
                "abandoned": ABANDONED_REASON_PREFIX,
                "pending": REPLAY_PENDING_REASON,
            },
        )


_LEDGER_ROW = (
    "SELECT id, org, scope_type, scope_value, requester, urgency, purge, requested_at, "
    "completed_at, steps_completed, attempts FROM loom.erasure_requests FINAL "
)


async def completed_since(point: datetime, *, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """The erasures that completed after `point`, requested within the live ledger's 89
    days (`LIVE_LEDGER`), oldest first: those a Postgres restore to `point` undid
    (`erasure.app.replay`). From the point's whole second, as `completed_at` keeps whole
    seconds: running again one that completed just before the point changes nothing its
    steps did not."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        _LEDGER_ROW
        + "WHERE status = 'completed' AND completed_at >= {point:DateTime} AND "
        + LIVE_LEDGER
        + " "
        "ORDER BY requested_at, org, id",
        params={"point": point.replace(microsecond=0)},
    )
    return [dict(r) for r in rows]


async def trusted_row(
    org: str,
    erasure_id: str,
    *,
    before: datetime,
    actor: str,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """The latest row of saga `erasure_id` a restore to `before` can be trusted with: one
    checkpointed before it, or one `actor` (the reconcile) wrote. A `BACKUP` copies its
    tables one after another from its start, so a step checkpointed after it may be in
    the restored ledger without its deletes in a table copied earlier. Its status
    ("completed", "abandoned" for the recovery worker's terminal row, else the stored
    one), `completed_at`, and `done`: the steps it names less those whose tables its
    last census found rows in (`census.steps_to_reopen`). None for no such row, which a
    merge that kept only a later version also leaves: every step then runs again."""
    from server.erasure.store.census import steps_to_reopen

    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT argMax(toString(status), version) AS status, "
        "argMax(completed_at, version) AS completed_at, "
        "argMax(failure_reason, version) AS failure_reason, "
        "argMax(steps_completed, version) AS steps_completed, "
        "argMax(residue, version) AS residue, any(scope_type) AS scope_type "
        "FROM loom.erasure_requests WHERE id = {id:UUID} AND org = {org:String} "
        "AND (last_checkpoint_at < {before:DateTime} OR actor = {actor:String}) AND "
        + LIVE_LEDGER
        + " "
        "HAVING count() > 0",
        params={
            "id": erasure_id,
            "org": org,
            "before": before.replace(microsecond=0),
            "actor": actor,
        },
    )
    if not row:
        return None
    status = str(row["status"])
    if status == "failed" and str(row["failure_reason"]).startswith(ABANDONED_REASON_PREFIX):
        status = "abandoned"
    scope = "user_id" if row["scope_type"] == "user_id" else "namespace"
    done = set(row["steps_completed"]) - steps_to_reopen(row["residue"], scope)
    return {"status": status, "completed_at": row["completed_at"], "done": done}


async def still_open(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """The erasures not completed and not abandoned: running, or awaiting the recovery
    worker, which resumes them."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        _LEDGER_ROW
        + "WHERE "
        + LIVE_LEDGER
        + " AND "
        + WILL_RUN
        + " ORDER BY requested_at, org, id",
        params={"abandoned": ABANDONED_REASON_PREFIX, "pending": REPLAY_PENDING_REASON},
    )
    return [dict(r) for r in rows]
