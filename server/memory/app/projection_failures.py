"""Durable logging for best-effort memory projection failures."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast

from server.core.ch import get_client
from server.core.ch_writes import async_insert_settings
from server.core.datetimes import (
    as_aware_utc,
    naive_utc_now,
    naive_utc_now_seconds,
    parse_iso_dt,
    to_naive_utc,
)
from server.core.errors import caller_text
from server.identity.domain.read_scope import (
    READ_SCOPE_KEY,
    WHOLE_NAMESPACE,
    RefusedReadScope,
    read_filters_bound,
    replayed_read_scope,
)
from server.llm import customer
from server.llm.identity import IdentityUnread, reads_writer
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult
from server.memory.app.projection_errors import (
    LANDED_ENTRIES,
    LANDED_RESULTS,
    TRIPLES_ARRIVED_AT,
    TRIPLES_ENTITY_MAP,
    TRIPLES_INSERTED_AT,
    TRIPLES_REPAIR,
    TRIPLES_REPAIRS,
    TRIPLES_RESTORE_ID,
    ProjectionScope,
    TriplesRepairFailed,
    repair_failure_payload,
    restated_repair_payload,
)
from server.memory.domain.graph_quality import IntervalRepair
from server.memory.domain.temporal_sql import HOLDS_VALUE_SQL
from server.memory.domain.trust import subject_of
from server.memory.store.rows import IdentityKey

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectionFailureRow:
    failure_id: str
    org: str
    namespace: str
    agent: str
    projection: str
    source_operation: str
    source_resource_id: str
    memory_keys: list[str]
    error_type: str
    error_message: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    created_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(frozen=True)
class ProjectionFailureRetryResult:
    failure_id: str
    projection: str
    retried: bool
    resolved: bool
    reason: str = ""
    # Given up on: no retry can do the work (`reason` says why).
    abandoned: bool = False
    error: str = ""


# The projections whose failures name supersession work rather than a derived table:
# the triple follow of retired rows, and the mirror of a retirement the re-read after
# the write could not confirm.
FOLLOW_ROWS = "triples_follow_rows"
# A follow owed before its retirement commits (the conflict resolver's): replayed as
# FOLLOW_ROWS. Its own name, since an older build replays FOLLOW_ROWS without checking
# that the loser is still retired, and leaves a name it does not know active.
FOLLOW_OWED = "triples_follow_owed"
# Triples a follow closed but could not re-check for a live row holding them.
TRIPLES_REOPEN = "triples_reopen"
# The facts of rows a revision or an Undo changed, not yet reconciled with them.
TRIPLES_RECONCILE = "triples_reconcile"
# The edges a revision or a late projection (memory.set, memory.bulk_set, the by-id
# updates) wrote for a value the row no longer holds, whose cleanup failed: payload
# memory_id, the entities the edges paired and, for a row that is not 'high',
# content_sha256 and (when it was read) ended_at. A 'high' row's names no value: the
# repair closes each of the row's values it no longer holds.
EDGES_CLOSE = "entity_edges_close"
# Rows a write made 'high' (or whose declared entities it narrowed) whose links to
# undeclared pairs are not dropped yet: payload versions (memory_id -> the version of
# that write, which dates the edges' end); the drop reads the row's current entities.
EDGES_DROP_UNDECLARED = "entity_edges_drop_undeclared"
# A drop whose restore of the links a write landing since it read them holds failed:
# payload links (memory_id, content_sha256, entity_a, entity_b, whether the DELETE took
# it) and close_ids, the arguments of the restore. Its own name: a replay of the drop skips a
# row no longer 'high', and that is the row whose links went.
EDGES_RESTORE = "entity_edges_restore"
# The facts of a row the sensitivity sweep relabels 'high', owed right before its commit
# (`facts_owed_payload`). Closed once ClickHouse, which the graph is judged from, holds
# the relabel; nothing is owed when it settled without it.
SENSITIVITY_FACTS = "sensitivity_facts_owed"
# Intents written (a waited async insert) right before the commit their writer then
# settles itself: the repair leaves one this long, so the two never settle it side by side.
_GRACED = (FOLLOW_OWED, SENSITIVITY_FACTS)
OWED_GRACE_S = 600  # not a setting: protocol timing between writer and repair


def facts_owed_payload(key: Sequence[str], memory_id: str, version: int) -> dict[str, Any]:
    """A SENSITIVITY_FACTS payload: the identity key, the row and the relabel's version."""
    return {"key": list(key), "memory_id": memory_id, "version": version}


# The `error_type` of a failure recorded with Loom-written text, not an exception.
TEXT_REASON = "-"


def projection_failure_row(
    *,
    org: str,
    namespace: str,
    agent: str = "",
    projection: str,
    source_operation: str = "",
    source_resource_id: str = "",
    memory_keys: list[str] | None = None,
    error: BaseException | str,
    payload: dict[str, Any] | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """One active projection failure row, a fresh failure_id. `user_id`: the writer's,
    kept in the payload so a user erasure finds the row."""
    payload = dict(payload or {})
    if user_id and not payload.get("user_id"):
        payload["user_id"] = user_id
    # "-" for a reason Loom wrote as text: no exception class has that name.
    error_type = type(error).__name__ if isinstance(error, BaseException) else TEXT_REASON
    return {
        "failure_id": str(uuid.uuid4()),
        "org": org,
        "namespace": namespace,
        "agent": agent,
        "projection": projection,
        "source_operation": source_operation,
        "source_resource_id": source_resource_id,
        "memory_keys": list(memory_keys or []),
        "error_type": error_type,
        "error_message": str(error)[:4000],
        "payload": json.dumps(payload, sort_keys=True, default=str),
        "status": "active",
        "created_at": naive_utc_now(),
        "resolved_at": None,
    }


async def insert_projection_failures(rows: list[dict[str, Any]], *, ch: Any | None = None) -> None:
    """Append `projection_failure_row`s in one waited async insert. Callers wrap this
    helper in their own fail-open guard."""
    if not rows:
        return
    await (ch or get_client()).insert(
        "loom.memory_projection_failures",
        rows,
        # Waited for: the row is the only record of the lost projection, and a
        # fire-and-forget buffer dies with the replica that holds it.
        settings=async_insert_settings(),
    )


async def record_projection_failure(
    *,
    org: str,
    namespace: str,
    agent: str = "",
    projection: str,
    source_operation: str = "",
    source_resource_id: str = "",
    memory_keys: list[str] | None = None,
    error: BaseException | str,
    payload: dict[str, Any] | None = None,
    ch: Any | None = None,
    user_id: str = "",
) -> str:
    """Append one projection failure row (`projection_failure_row`); returns its
    failure_id. Callers wrap this helper in their own fail-open guard."""
    row = projection_failure_row(
        org=org,
        namespace=namespace,
        agent=agent,
        projection=projection,
        source_operation=source_operation,
        source_resource_id=source_resource_id,
        memory_keys=memory_keys,
        error=error,
        payload=payload,
        user_id=user_id,
    )
    await insert_projection_failures([row], ch=ch)
    return str(row["failure_id"])


def _decode_payload(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    try:
        obj = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _row_from_ch(row: dict[str, Any]) -> ProjectionFailureRow:
    return ProjectionFailureRow(
        failure_id=str(row.get("failure_id") or ""),
        org=str(row.get("org") or ""),
        namespace=str(row.get("namespace") or ""),
        agent=str(row.get("agent") or ""),
        projection=str(row.get("projection") or ""),
        source_operation=str(row.get("source_operation") or ""),
        source_resource_id=str(row.get("source_resource_id") or ""),
        memory_keys=[str(k) for k in list(row.get("memory_keys") or []) if str(k)],
        error_type=str(row.get("error_type") or ""),
        error_message=str(row.get("error_message") or ""),
        payload=_decode_payload(row.get("payload")),
        status=str(row.get("status") or "active"),
        created_at=(c if isinstance(c := row.get("created_at"), datetime) else None),
        resolved_at=(r if isinstance(r := row.get("resolved_at"), datetime) else None),
    )


async def list_projection_failures(
    *,
    org: str,
    namespace: str | None = None,
    projection: str = "",
    status: str = "active",
    limit: int = 100,
    scope_where: str = "",
    scope_params: dict[str, Any] | None = None,
    ch: Any | None = None,
) -> list[ProjectionFailureRow]:
    """List durable projection failures newest-first. `scope_where` (a fragment that
    starts with ` AND `) and `scope_params` narrow it to the caller's namespaces
    (`resolve_org_scope`)."""

    ch = ch or get_client()
    where = ["org = {org:String}"]
    params: dict[str, Any] = {**(scope_params or {}), "org": org, "lim": int(limit)}
    if namespace:
        where.append("namespace = {ns:String}")
        params["ns"] = namespace
    if projection:
        where.append("projection = {projection:String}")
        params["projection"] = projection
    if status:
        where.append("status = {status:String}")
        params["status"] = status
    rows = await ch.query_all(
        f"""
        SELECT
            toString(failure_id) AS failure_id,
            org,
            namespace,
            agent,
            projection,
            source_operation,
            source_resource_id,
            memory_keys,
            error_type,
            error_message,
            payload,
            status,
            created_at,
            resolved_at
        FROM loom.memory_projection_failures
        WHERE {" AND ".join(where)}{scope_where}
        ORDER BY created_at DESC, failure_id DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return [_row_from_ch(dict(row)) for row in rows]


async def mark_projection_failures_resolved(
    *,
    org: str | Sequence[str],
    failure_ids: list[str],
    scope_where: str = "",
    scope_params: dict[str, Any] | None = None,
    ch: Any | None = None,
) -> int:
    """Mark failures resolved with one ALTER UPDATE, narrow by UUID, in `org` (one, or
    several for a batch of them), within `scope_where` as `list_projection_failures`
    takes it. Callers batch the ids (`MARK_CHUNK`): every point write that retires rows
    leaves an intent here, so a mutation per id would be one per write. Resolved rows
    leave by the table's TTL."""

    ids = [fid for fid in failure_ids if fid]
    if not ids:
        return 0
    orgs = [org] if isinstance(org, str) else sorted(set(org))
    ch = ch or get_client()
    await ch.execute(
        f"""
        ALTER TABLE loom.memory_projection_failures
        UPDATE status = 'resolved', resolved_at = {{at:DateTime}}
        WHERE org IN {{orgs:Array(String)}}
          AND toString(failure_id) IN {{failure_ids:Array(String)}}
          AND status = 'active'{scope_where}
        """,
        # bound: a replicated table refuses now() in a mutation
        params={
            **(scope_params or {}),
            "orgs": orgs,
            "failure_ids": ids,
            "at": naive_utc_now_seconds(),
        },
    )
    return len(ids)


async def mark_projection_failures_abandoned(
    *, org: str, failure_ids: list[str], reason: str, ch: Any | None = None
) -> None:
    """Take failures no retry can repair out of the active queue as `abandoned`,
    their `reason` added to what the write recorded, for an operator to look at."""
    ch = ch or get_client()
    await ch.execute(
        """
        ALTER TABLE loom.memory_projection_failures
        UPDATE status = 'abandoned',
               error_message = concat(error_message, '; abandoned: ', {reason:String})
        WHERE org = {org:String}
          AND toString(failure_id) IN {failure_ids:Array(String)}
          AND status = 'active'
        """,
        params={"org": org, "failure_ids": failure_ids, "reason": reason},
    )


# A failure and the result of its retry.
Retried = tuple[ProjectionFailureRow, ProjectionFailureRetryResult]
# Failure ids per mark: the ids travel as one HTTP parameter, which ClickHouse caps at
# 128 KiB (about 3,400 UUIDs).
MARK_CHUNK = 1000


async def retry_failures(
    failures: list[ProjectionFailureRow], *, ch: Any | None = None
) -> list[ProjectionFailureRetryResult]:
    """Retry each failure, then mark what it repaired or gave up on (`mark_retried`). A
    retry cut short leaves the rest active, and every kind is safe to replay."""
    ch = ch or get_client()
    done: list[Retried] = []
    try:
        for failure in failures:
            try:
                with customer.for_org(
                    failure.org,
                    namespace=failure.namespace,
                    subject=(
                        ""
                        if failure.projection == "entity_vectors"
                        else await _writer(failure, ch=ch)
                    ),
                    user_id=str(failure.payload.get("user_id") or ""),
                ):
                    retry = await _retry(failure, ch=ch)
            except customer.GROUP_HOLDS:
                retry = _held(failure)
            done.append((failure, retry))
    finally:
        await mark_retried(done, ch=ch)
    return [r for _, r in done]


async def _writer(failure: ProjectionFailureRow, *, ch: Any | None = None) -> str:
    """The principal whose memories a model-bearing projection replays."""
    if (
        failure.projection not in {"memory_chunks", "entity_vectors"}
        or not failure.memory_keys
        or not reads_writer()
    ):
        return ""
    import server.memory.store as memory_repo

    keys = [(failure.namespace, str(k)) for k in failure.memory_keys]
    found = await memory_repo.actors_by_key(failure.org, keys, scope=WHOLE_NAMESPACE, ch=ch)
    wanted = {c: str(failure.payload.get(c) or "-") for c in ("user_id", "session_id")}
    if failure.projection == "memory_chunks":
        wanted["session_id"] = _session_from_source_resource_id(failure.source_resource_id)
        wanted.pop("user_id")
    subjects = set()
    for key in keys:
        for head in found.get(key, []):
            if head["agent"] != failure.agent:
                continue
            if any(head[c] != value for c, value in wanted.items()):
                continue
            if subject := subject_of(head["actor"]):
                subjects.add(subject)
    if len(subjects) > 1:
        raise IdentityUnread("the projection's memories name more than one principal")
    return next(iter(subjects), "")


def _held(failure: ProjectionFailureRow) -> ProjectionFailureRetryResult:
    return ProjectionFailureRetryResult(
        failure_id=failure.failure_id,
        projection=failure.projection,
        retried=False,
        resolved=False,
        reason="model_calls_held",
    )


async def mark_retried(done: list[Retried], *, ch: Any | None = None) -> None:
    """Mark the repaired failures of `done` per org and the abandoned ones per org and
    reason, `MARK_CHUNK` ids to a mutation."""
    ch = ch or get_client()
    by_org_reason: dict[tuple[str, str], list[str]] = {}
    for failure, r in done:
        if r.resolved or r.abandoned:
            by_org_reason.setdefault((failure.org, r.reason), []).append(r.failure_id)
    for (org, reason), ids in by_org_reason.items():
        if not reason:
            await mark_resolved(org, ids, ch=ch)
            continue
        for start in range(0, len(ids), MARK_CHUNK):
            chunk = ids[start : start + MARK_CHUNK]
            await mark_projection_failures_abandoned(
                org=org, failure_ids=chunk, reason=reason, ch=ch
            )


async def mark_resolved(org: str | Sequence[str], ids: list[str], *, ch: Any | None = None) -> None:
    """`mark_projection_failures_resolved`, `MARK_CHUNK` ids to a mutation."""
    for start in range(0, len(ids), MARK_CHUNK):
        chunk = ids[start : start + MARK_CHUNK]
        await mark_projection_failures_resolved(org=org, failure_ids=chunk, ch=ch)


def _pending(failure: ProjectionFailureRow) -> ProjectionFailureRetryResult:
    """Not retried yet: the failure stays active for a later pass."""
    return ProjectionFailureRetryResult(
        failure_id=failure.failure_id,
        projection=failure.projection,
        retried=False,
        resolved=False,
        reason="pending",
    )


async def _retry(failure: ProjectionFailureRow, *, ch: Any) -> ProjectionFailureRetryResult:
    """One failure's retry, unmarked. Every kind writes in the failure's org: under an
    erasure it waits (`pending`), and once the saga ends its replay finds an erased row
    gone."""
    from server.memory.store.conflict_resolver import under_erasure

    reason = ""
    try:
        if failure.projection in _GRACED and _younger_than(failure, OWED_GRACE_S):
            return _pending(failure)
        # Read for each failure, right before its write: an erasure can be requested
        # mid-batch. A failed read raises below and leaves the row active.
        if await under_erasure(failure.org, ch=ch):
            return _pending(failure)
        if failure.projection == "memory_belief_sources":
            await _retry_belief_sources(failure, ch=ch)
        elif failure.projection == "memory_chunks":
            await _retry_memory_chunks(failure, ch=ch)
        elif failure.projection == "memory_chunk_sources":
            await _retry_memory_chunk_sources(failure, ch=ch)
        elif failure.projection in {
            "entity_vectors",
            "entity_edges",
            "entity_triples",
            TRIPLES_REPAIR,
        }:
            reason = await _retry_graph_projection(failure, ch=ch)
        elif failure.projection in (FOLLOW_ROWS, FOLLOW_OWED):
            reason = await _retry_follow_rows(failure, ch=ch)
        elif failure.projection == TRIPLES_REOPEN:
            reason = await _retry_triples_reopen(failure, ch=ch)
        elif failure.projection == TRIPLES_RECONCILE:
            reason = await _retry_triples_reconcile(failure, ch=ch)
        elif failure.projection == EDGES_CLOSE:
            reason = await _retry_edges_close(failure, ch=ch)
        elif failure.projection == EDGES_DROP_UNDECLARED:
            reason = await _retry_edges_drop(failure, ch=ch)
        elif failure.projection == EDGES_RESTORE:
            reason = await _retry_edges_restore(failure, ch=ch)
        elif failure.projection == SENSITIVITY_FACTS:
            outcome = await _retry_sensitivity_facts(failure, ch=ch)
            if outcome is None:
                return _pending(failure)
            reason = outcome
        else:
            return ProjectionFailureRetryResult(
                failure_id=failure.failure_id,
                projection=failure.projection,
                retried=False,
                resolved=False,
                reason="unsupported_projection",
            )
        return ProjectionFailureRetryResult(
            failure_id=failure.failure_id,
            projection=failure.projection,
            retried=True,
            resolved=not reason,
            reason=reason,
            abandoned=bool(reason),
        )
    except customer.HOLDS:
        # Held by the model-call switch: no attempt, no error; the row stays open.
        return _held(failure)
    except Exception as e:  # noqa: BLE001 — reported on the result; the row stays open
        # `ops.projection_failures.retry` returns the error: its kind; the text is logged.
        log.warning(
            "projection failure %s: the retry failed: %s: %s",
            failure.failure_id,
            type(e).__name__,
            e,
        )
        return ProjectionFailureRetryResult(
            failure_id=failure.failure_id,
            projection=failure.projection,
            retried=True,
            resolved=False,
            error=caller_text(e),
        )


async def retry_projection_failures(
    *,
    org: str,
    namespace: str | None = None,
    projection: str = "",
    limit: int = 50,
    scope_where: str = "",
    scope_params: dict[str, Any] | None = None,
    ch: Any | None = None,
) -> list[ProjectionFailureRetryResult]:
    """Retry active failures in newest-first order, within `scope_where` as
    `list_projection_failures` takes it."""

    ch = ch or get_client()
    rows = await list_projection_failures(
        org=org,
        namespace=namespace,
        projection=projection,
        status="active",
        limit=limit,
        scope_where=scope_where,
        scope_params=scope_params,
        ch=ch,
    )
    return await retry_failures(rows, ch=ch)


async def scan_active_projection_failures(
    *,
    limit: int = 50,
    exclude_orgs: frozenset[str] = frozenset(),
    after: tuple[datetime, str] | None = None,
    ch: Any | None = None,
) -> list[ProjectionFailureRow]:
    """Return active failures across orgs for the repair worker, oldest first. Failures
    a retry would only answer `pending` for are left out, so they hold no place ahead of
    the rest: those of an org under erasure, follow and owed-facts intents within their
    grace, and those of `exclude_orgs` (orgs whose model calls are switched off), which
    wait."""
    from server.memory.store.conflict_resolver import ERASING_ORGS_SQL

    ch = ch or get_client()
    params: dict[str, Any] = {
        "lim": int(limit),
        "off": sorted(exclude_orgs),
        "graced": list(_GRACED),
        "grace": OWED_GRACE_S,
    }
    cursor_sql = ""
    if after is not None:
        cursor_sql = "AND (f.created_at, f.failure_id) > ({after_at:DateTime}, {after_id:UUID})"
        params.update(after_at=as_aware_utc(after[0]), after_id=after[1])
    rows = await ch.query_all(
        f"""
        SELECT
            toString(f.failure_id) AS failure_id,
            org,
            namespace,
            agent,
            projection,
            source_operation,
            source_resource_id,
            memory_keys,
            error_type,
            error_message,
            payload,
            status,
            created_at,
            resolved_at
        FROM loom.memory_projection_failures AS f
        WHERE status = 'active'
          AND org NOT IN ({ERASING_ORGS_SQL})
          AND org NOT IN {{off:Array(String)}}
          {cursor_sql}
          AND NOT (projection IN {{graced:Array(String)}}
                   AND created_at > now() - INTERVAL {{grace:UInt32}} SECOND)
        ORDER BY f.created_at ASC, f.failure_id ASC
        LIMIT {{lim:UInt32}}
        SETTINGS select_sequential_consistency = 1
        """,
        params=params,
    )
    return [_row_from_ch(dict(row)) for row in rows]


async def _retry_belief_sources(
    failure: ProjectionFailureRow,
    *,
    ch: Any,
) -> None:
    evidence_ids = [str(eid) for eid in list(failure.payload.get("evidence_ids") or []) if str(eid)]
    if not evidence_ids or not failure.memory_keys:
        raise ValueError("memory_belief_sources retry requires memory_keys and evidence_ids")
    rows = await ch.query_all(
        """
        SELECT memory_key, toString(memory_id) AS memory_id
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND memory_key IN {keys:Array(String)}
          AND tombstoned_at IS NULL
        """,
        params={
            "org": failure.org,
            "ns": failure.namespace,
            "agent": failure.agent,
            "keys": failure.memory_keys,
        },
    )
    links = [
        (str(row["memory_key"]), str(row["memory_id"])) for row in rows if row.get("memory_id")
    ]
    if not links:
        raise ValueError("no live memories found for belief-source retry")
    from server.memory.app.evidence import link_belief_sources

    await link_belief_sources(
        org=failure.org,
        namespace=failure.namespace,
        memory_links=links,
        evidence_ids=evidence_ids,
        support_type="extracted_from",
        ch=ch,
    )


async def _retry_follow_rows(failure: ProjectionFailureRow, *, ch: Any) -> str:
    """Follow the supersession pairs a write left for later, reading each chain to
    its end. A pair still unread is a cycle or a chain past `REPAIR_CHAIN_HOPS`,
    which no retry reads: the reason returned abandons the failure, as it does for
    a payload no retry can read."""

    from server.memory.app.triple_follow import REPAIR_CHAIN_HOPS, follow_row_supersessions

    try:
        pairs = [(str(o), str(n)) for o, n in failure.payload["pairs"]]
        if not pairs:
            raise ValueError("no pairs")
    except (KeyError, TypeError, ValueError):
        return "malformed_payload"
    scope = _failure_scope(failure)
    async with _recording_restated_repair(failure, ch=ch):
        await _finish_restated_repair(failure, scope, ch=ch)
        outcome = await follow_row_supersessions(scope, pairs, ch=ch, hops=REPAIR_CHAIN_HOPS)
    if failure.projection == FOLLOW_OWED:
        await _record_owed_retirements(failure, ch=ch)
    if not outcome.unfollowed:
        return ""
    log.warning(
        "projection repair: %d supersession pair(s) left unfollowed in %s/%s",
        len(outcome.unfollowed),
        failure.org,
        failure.namespace,
    )
    return f"unfollowable_chain: {len(outcome.unfollowed)} pair(s)"


def _younger_than(failure: ProjectionFailureRow, seconds: float) -> bool:
    created = failure.created_at
    return created is not None and (
        (naive_utc_now() - to_naive_utc(created)).total_seconds() <= seconds
    )


async def _record_owed_retirements(failure: ProjectionFailureRow, *, ch: Any) -> None:
    """Write the missing record of each retirement in the intent still in place, and clear
    the winner's flag against its loser: the writer stopped before doing so. A failed
    emit raises, so the intent stays for the next replay."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.app.auto_resolution import (
        EVENT_TYPE,
        record_auto_supersessions,
    )
    from server.memory.app.conflict_resolver import clear_retired_flag
    from server.memory.store import get_by_id

    for entry in failure.payload.get("retired") or []:
        loser, winner = str(entry["memory_id"]), str(entry["superseded_by"])
        head = await get_by_id(
            loser,
            org=failure.org,
            namespace=failure.namespace,
            ch=ch,
            scope=WHOLE_NAMESPACE,
            strong=True,
        )
        if head is None or head.status != "superseded" or str(head.superseded_by) != winner:
            continue
        await clear_retired_flag(failure.org, failure.namespace, winner, loser)
        found = await ch.query_one(
            "SELECT count() AS n FROM loom.audit_log"
            " WHERE org = {org:String} AND namespace = {ns:String} AND event_type = {e:String}"
            # The record names its intent (request_id): an earlier, undone retirement
            # of the same pair has another.
            " AND request_id = {intent:String} AND position(subject, {loser:String}) > 0"
            " SETTINGS select_sequential_consistency = 1",
            params={
                "org": failure.org,
                "ns": failure.namespace,
                "e": EVENT_TYPE,
                "intent": failure.failure_id,
                "loser": loser,
            },
        )
        if found and int(found["n"]):
            continue
        if not await record_auto_supersessions(
            org=failure.org,
            namespace=failure.namespace,
            retired=[entry],
            reason=str(failure.payload["reason"]),
            request_id=failure.failure_id,
        ):
            raise RuntimeError("the retirement's record was not written")


async def _retry_triples_reopen(failure: ProjectionFailureRow, *, ch: Any) -> str:
    """Undo a follow's close for the triples a live row holds, as the follow would
    have. A payload no retry can read returns the reason that abandons it."""
    from server.memory.app.triple_projections import reopen_held

    try:
        closes = [
            (str(s), str(r), str(o), datetime.fromisoformat(at))
            for s, r, o, at in failure.payload["closes"]
        ]
        closed_at = datetime.fromisoformat(failure.payload["closed_at"])
    except (KeyError, TypeError, ValueError):
        return "malformed_payload"
    held = await reopen_held(
        org=failure.org, namespace=failure.namespace, closes=closes, closed_at=closed_at, ch=ch
    )
    if held:
        log.info(
            "projection repair: opened again %d triple(s) a live row holds in %s/%s",
            len(held),
            failure.org,
            failure.namespace,
        )
    return ""


async def _retry_edges_close(failure: ProjectionFailureRow, *, ch: Any) -> str:
    """Run the failed cleanup of a late projection again. A payload no retry can
    read returns the reason that abandons it."""
    from server.memory.app.edge_repair import close_late_edges, close_overtaken_values

    memory_id = str(failure.payload.get("memory_id") or "")
    content_sha256 = str(failure.payload.get("content_sha256") or "")
    if not memory_id:
        return "malformed_payload"
    if not content_sha256:
        # A high row's close names no value: each one it no longer holds closes.
        await close_overtaken_values(failure.org, failure.namespace, memory_id, ch=ch)
        return ""
    # When the revision ended the value; a payload recorded before it was known has none.
    ended = failure.payload.get("ended_at")
    try:
        ended_at = datetime.fromisoformat(ended) if ended else None
    except (TypeError, ValueError):
        return "malformed_payload"
    entities = [str(e) for e in failure.payload.get("entities") or []]
    await close_late_edges(
        failure.org,
        failure.namespace,
        memory_id,
        content_sha256,
        ended_at=ended_at,
        entities=entities,
        ch=ch,
    )
    return ""


def _failure_scope(failure: ProjectionFailureRow) -> ProjectionScope:
    """The scope a replay runs as: the failure's own, with the user its payload names."""
    return ProjectionScope(
        org=failure.org,
        namespace=failure.namespace,
        agent=failure.agent,
        user_id=str(failure.payload.get("user_id") or ""),
    )


async def _retry_edges_drop(failure: ProjectionFailureRow, *, ch: Any) -> str:
    """Drop again the undeclared edge links of the rows a write made 'high'; a failure
    raises and leaves the row active."""

    from server.memory.app.edge_links import drop_undeclared_links

    # The version of the write that owes the drop, per row, dates the edges' end.
    versions = failure.payload.get("versions")
    if not isinstance(versions, dict) or not versions:
        return "malformed_payload"
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in versions.values()):
        return "malformed_payload"
    scope = _failure_scope(failure)
    await drop_undeclared_links(
        scope,
        {str(mid): int(v) for mid, v in versions.items()},
        ch=ch,
        op=failure.source_operation,
    )
    return ""


async def _retry_edges_restore(failure: ProjectionFailureRow, *, ch: Any) -> str:
    """Write back the links a drop deleted that their row holds now; a failure raises and
    leaves the row active."""
    from server.memory.app.edge_links import restore_kept_links

    links = failure.payload.get("links")
    if not isinstance(failure.payload.get("close_ids"), list) or not isinstance(links, list):
        return "malformed_payload"
    if not all(isinstance(link, list) and len(link) == 5 for link in links):
        return "malformed_payload"
    await restore_kept_links(
        failure.payload, _failure_scope(failure), ch=ch, op=failure.source_operation
    )
    return ""


async def _retry_sensitivity_facts(failure: ProjectionFailureRow, *, ch: Any) -> str | None:
    """Close the facts of a row relabelled 'high' (`close_owed_facts`). Returns "" once
    nothing is owed, None while ClickHouse lacks the relabel (pending), and
    "malformed_payload" for a payload no retry can read; a failed close raises."""
    from server.memory.app.sensitivity_sweep import close_owed_facts

    key, memory_id, version = (failure.payload.get(k) for k in ("key", "memory_id", "version"))
    if not (isinstance(key, list) and len(key) == 6 and all(isinstance(k, str) for k in key)):
        return "malformed_payload"
    if not isinstance(memory_id, str) or not isinstance(version, int) or isinstance(version, bool):
        return "malformed_payload"
    if not await close_owed_facts(cast(IdentityKey, tuple(key)), memory_id, version, ch=ch):
        return None
    return ""


def _recorded_repairs(payload: dict[str, Any]) -> list[IntervalRepair]:
    """The closes a failed interval repair had made, as `repair_failure_payload` stored them."""
    return [
        IntervalRepair(
            subject=r["subject"],
            relation=r["relation"],
            old_object=r["old_object"],
            new_object=r["new_object"],
            valid_to=datetime.fromisoformat(r["valid_to"]),
            valid_from=parse_iso_dt(r["valid_from"]),
            invalid_at=parse_iso_dt(r["invalid_at"]),
            new_valid_from=parse_iso_dt(r["new_valid_from"]),
            new_seen_at=parse_iso_dt(r["new_seen_at"]),
        )
        for r in payload.get(TRIPLES_REPAIRS) or []
    ]


def _recorded_entity_map(payload: dict[str, Any]) -> dict[str, str] | None:
    """What the failed write resolved its names to. None for a failure recorded
    before the payload carried it: the replay resolves the names again."""
    names = payload.get(TRIPLES_ENTITY_MAP)
    if not isinstance(names, dict):
        return None
    return {str(k): str(v) for k, v in names.items()}


@asynccontextmanager
async def _recording_restated_repair(
    failure: ProjectionFailureRow, *, ch: Any
) -> AsyncIterator[None]:
    """A replay whose repair of an Undo's or a fallback's markers fails records them and
    the closes it made, as the write did, so the next replay finishes that repair. Without
    them it would find the markers stated and the relation looking settled, and repair
    nothing."""
    try:
        yield
    except TriplesRepairFailed as e:
        await _record_changes(failure, restated_repair_payload(e), ch=ch)
        raise


async def _finish_restated_repair(failure: ProjectionFailureRow, scope: Any, *, ch: Any) -> None:
    """First finish the repair of an Undo's or a fallback's markers that the failure
    recorded, with the closes it had made: the relation may look settled since, and the
    rest of the replay would then repair nothing."""
    restore_id = str(failure.payload.get(TRIPLES_RESTORE_ID) or "")
    restored_at = parse_iso_dt(failure.payload.get(TRIPLES_INSERTED_AT) or "")
    if not restore_id or restored_at is None:
        return
    from server.memory.app.graph_undo import finish_restated_repair

    await finish_restated_repair(
        scope,
        restore_id,
        restored_at=restored_at,
        repaired=_recorded_repairs(failure.payload),
        ch=ch,
    )


async def _retry_triples_reconcile(failure: ProjectionFailureRow, *, ch: Any) -> str:
    """Reconcile the facts of the rows a revision or an Undo changed, as it would have."""

    from server.memory.app.graph_undo import reopen_held_facts
    from server.memory.app.triple_projections import close_dropped_facts

    ids = [str(i) for i in failure.payload.get("memory_ids") or []]
    if not ids:
        return "malformed_payload"
    scope = _failure_scope(failure)
    async with _recording_restated_repair(failure, ch=ch):
        await _finish_restated_repair(failure, scope, ch=ch)
        if failure.payload.get("reopen"):
            # As of the Undo, by its id: a value stated since stays the newer one, and ''
            # is every other row's id.
            restored_at = parse_iso_dt(failure.payload.get("restored_at") or "")
            restore_id = str(failure.payload.get("restore_id") or "")
            if restored_at is None or not restore_id:
                return "malformed_payload"
            # A row revised since the Undo is its revision's to reconcile, not the Undo's.
            hashes = failure.payload.get("content_sha256") or {}
            await reopen_held_facts(
                scope, ids, ch=ch, restored_at=restored_at, restore_id=restore_id, hashes=hashes
            )
        else:
            await close_dropped_facts(scope, ids, ch=ch)
    return ""


async def _retry_memory_chunks(
    failure: ProjectionFailureRow,
    *,
    ch: Any,
) -> None:
    rows = await ch.query_all(
        """
        SELECT role, content, user_id
        FROM (
            SELECT role, content, user_id, observed_at, evidence_id, tombstoned_at
            FROM loom.memory_evidence
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND agent = {agent:String}
              AND source_resource_id = {source:String}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL
        ORDER BY observed_at ASC, evidence_id ASC
        """,
        params={
            "org": failure.org,
            "ns": failure.namespace,
            "agent": failure.agent,
            "source": failure.source_resource_id,
        },
    )
    msgs = [
        {"role": str(row.get("role") or "user"), "content": str(row.get("content") or "")}
        for row in rows
        if str(row.get("content") or "").strip()
    ]
    if not msgs:
        raise ValueError("memory_chunks retry requires source evidence rows")
    # The chunks belong to the end user the source turns were written under;
    # turns of one source under several users leave the sentinel (a
    # user-bounded read then excludes the chunks rather than guess).
    users = {str(row.get("user_id") or "-") for row in rows}
    user_id = users.pop() if len(users) == 1 else "-"

    from server.memory.app.chunk_projection import write_memory_chunks

    retry_input = type(
        "_ProjectionRetryInput",
        (),
        {
            "org": failure.org,
            "namespace": failure.namespace,
            "agent": failure.agent,
            "user_id": user_id,
            "session_id": _session_from_source_resource_id(failure.source_resource_id),
        },
    )()
    with customer.attributed(replace(customer.attribution(), user_id=user_id)):
        await write_memory_chunks(
            retry_input,
            msgs,
            parent_memory_keys=failure.memory_keys,
        )


async def _retry_memory_chunk_sources(
    failure: ProjectionFailureRow,
    *,
    ch: Any,
) -> None:
    source = str(failure.payload.get("source_resource_id") or failure.source_resource_id)
    session_id = str(failure.payload.get("session_id") or _session_from_source_resource_id(source))
    chunk_rows = await ch.query_all(
        """
        SELECT
            chunk_index,
            content_sha256,
            line_start,
            line_end
        FROM loom.memory_chunks FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND session_id = {sid:String}
          AND tombstoned_at IS NULL
        ORDER BY chunk_index ASC, content_sha256 ASC
        """,
        params={
            "org": failure.org,
            "ns": failure.namespace,
            "agent": failure.agent,
            "sid": session_id,
        },
    )
    if not chunk_rows:
        raise ValueError("memory_chunk_sources retry requires chunk rows")
    evidence_rows = await ch.query_all(
        """
        SELECT toString(evidence_id) AS evidence_id, role, content
        FROM (
            SELECT evidence_id, role, content, observed_at, tombstoned_at
            FROM loom.memory_evidence
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND agent = {agent:String}
              AND source_resource_id = {source:String}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL
        ORDER BY observed_at ASC, evidence_id ASC
        """,
        params={
            "org": failure.org,
            "ns": failure.namespace,
            "agent": failure.agent,
            "source": source,
        },
    )
    msgs = [
        {"role": str(row.get("role") or "user"), "content": str(row.get("content") or "")}
        for row in evidence_rows
    ]
    evidence_ids = [str(row.get("evidence_id") or "") for row in evidence_rows]
    from server.memory.app.chunk_projection import message_line_evidence_refs

    refs = message_line_evidence_refs(msgs, evidence_ids)
    now = naive_utc_now()
    # The same builder as the write path.
    from server.memory.app.chunk_links import SOURCES_TABLE
    from server.memory.app.chunk_links import provenance_rows as _provenance_rows

    rows = _provenance_rows(
        org=failure.org,
        namespace=failure.namespace,
        agent=failure.agent,
        session_id=session_id,
        chunks=[
            (
                int(chunk.get("chunk_index") or 0),
                str(chunk.get("content_sha256") or ""),
                int(chunk.get("line_start") or 0),
                int(chunk.get("line_end") or 0),
            )
            for chunk in chunk_rows
        ],
        parent_keys=list(failure.memory_keys or []),
        evidence_refs=[(int(a), int(b), str(e)) for a, b, e in refs],
        now=now,
    )
    if not rows:
        raise ValueError("memory_chunk_sources retry produced no rows")
    await ch.insert(
        SOURCES_TABLE,
        rows,
        settings={"async_insert": 1, "wait_for_async_insert": 0},
    )


def _session_from_source_resource_id(source_resource_id: str) -> str:
    parts = str(source_resource_id or "").split(":")
    if len(parts) >= 2 and parts[0] == "memory.set_from_messages":
        return parts[1] or "-"
    return "-"


async def _retry_graph_projection(
    failure: ProjectionFailureRow,
    *,
    ch: Any,
) -> str:
    """Replay a bulk write's graph projection, for the entries whose row still holds
    the value they wrote. A triples replay runs within the writer's read scope; it
    returns the reason that abandons the failure when that scope was refused or
    incomplete."""
    # A replay that inserted the triples moved them out of the build before's reach.
    entries = failure.payload.get(LANDED_ENTRIES, failure.payload.get("entries"))
    results = failure.payload.get(LANDED_RESULTS, failure.payload.get("results"))
    if not isinstance(entries, list) or not isinstance(results, list):
        raise ValueError("graph projection retry requires entries/results payload")

    from server.embedding import get_embedder
    from server.memory.app.projections import (
        value_sha256,
        write_entity_edges,
        write_entity_vectors,
    )

    bulk_input = MemoryBulkSetInput(
        org=failure.org,
        namespace=failure.namespace,
        agent=failure.agent,
        user_id=str(failure.payload.get("user_id") or "-"),
        session_id=str(failure.payload.get("session_id") or "-"),
        run_id=str(failure.payload.get("run_id") or "-"),
        entries=[
            _BulkSetEntry(**entry)
            for entry in entries
            if isinstance(entry, dict) and entry.get("memory_key")
        ],
    )
    bulk_results = [
        _BulkSetResult(**result)
        for result in results
        if isinstance(result, dict) and result.get("memory_key")
    ]
    if not bulk_input.entries or not bulk_results:
        raise ValueError("graph projection retry has no valid entries/results")
    dedup_token = f"projection-repair:{failure.failure_id}"
    scope = WHOLE_NAMESPACE
    if failure.projection in ("entity_triples", TRIPLES_REPAIR):
        try:
            scope = replayed_read_scope(failure.payload.get(READ_SCOPE_KEY))
        except RefusedReadScope:
            return "refused_read_scope"
        except ValueError:
            return "incomplete_read_scope"
    # A replay that writes rows writes them only for rows that still hold the value:
    # one revised, retired or made 'high' since keeps what it holds now. A fact dated
    # in the past or a proposal is projected, as the write projected it. A repair of
    # triples that already landed is dated at their write and runs as it would have,
    # and so does a result recorded before results named their row: it has no id to check.
    kept: dict[str, Any] = {}
    if not failure.payload.get(TRIPLES_INSERTED_AT):
        from server.memory.app.graph_holders import versions_held

        pairs = list(zip(bulk_input.entries, bulk_results, strict=True))
        # By value: one bulk can revise a row twice, and only its last value is held.
        held = await versions_held(
            failure.org,
            failure.namespace,
            [(r.memory_id, value_sha256(e)) for e, r in pairs if r.memory_id],
            ch=ch,
            holder=(HOLDS_VALUE_SQL, {}),
        )
        pairs = [
            (e, r) for e, r in pairs if not r.memory_id or (r.memory_id, value_sha256(e)) in held
        ]
        if not pairs:
            return ""
        bulk_input = bulk_input.model_copy(update={"entries": [e for e, _r in pairs]})
        bulk_results = [r for _e, r in pairs]
        kept = {
            "entries": [e.model_dump(mode="json") for e in bulk_input.entries],
            "results": [r.model_dump(mode="json") for r in bulk_results],
        }
    # Bound for the auth the replay runs under: the worker's none, or the admin's
    # token when the ops op retries it.
    from server.auth.context import current_auth

    with read_filters_bound(current_auth(), scope):
        if failure.projection == "entity_vectors":
            replay = replace(failure, memory_keys=[e.memory_key for e in bulk_input.entries])
            with customer.attributed(
                replace(customer.attribution(), subject=await _writer(replay, ch=ch))
            ):
                await write_entity_vectors(bulk_input, bulk_results, get_embedder(), ch=ch)
        # entity_edges / entity_triples are plain MergeTree, one row per
        # observation: a replay that inserted and then failed to resolve would
        # count its rows again next pass. The failure's id is the dedup token.
        elif failure.projection == "entity_edges":
            await write_entity_edges(bulk_input, bulk_results, ch=ch, dedup_token=dedup_token)
        else:
            await _replay_triples(
                failure, bulk_input, bulk_results, kept, dedup_token=dedup_token, ch=ch
            )
    return ""


async def _replay_triples(
    failure: ProjectionFailureRow,
    bulk_input: MemoryBulkSetInput,
    bulk_results: list[_BulkSetResult],
    kept: dict[str, Any],
    *,
    dedup_token: str,
    ch: Any,
) -> None:
    """Replay a triples failure: insert the triples unless they landed, then repair."""
    from server.memory.app.projections import write_entity_triples

    landed = bool(failure.payload.get(TRIPLES_INSERTED_AT))
    try:
        await write_entity_triples(
            bulk_input,
            bulk_results,
            ch=ch,
            dedup_token=dedup_token,
            arrived_at=parse_iso_dt(failure.payload.get(TRIPLES_ARRIVED_AT) or ""),
            inserted_at=parse_iso_dt(failure.payload.get(TRIPLES_INSERTED_AT) or ""),
            repaired=_recorded_repairs(failure.payload),
            entity_map=_recorded_entity_map(failure.payload),
        )
    except TriplesRepairFailed as e:
        # The closes this replay made ride the failure, for the next to finish.
        changes = repair_failure_payload(e)
        if landed:
            await _record_changes(failure, changes, ch=ch)
            raise
        # This replay inserted the triples (the entries it kept). One mutation records
        # that in the failure before anything can replay it, so no replay inserts them
        # again. They move to keys the build before does not read: it would insert them.
        changes[LANDED_ENTRIES] = kept["entries"]
        changes[LANDED_RESULTS] = kept["results"]
        await _update_payload(failure, changes, drop=("entries", "results"), ch=ch)
        raise


async def _record_changes(
    failure: ProjectionFailureRow, changes: dict[str, Any], *, ch: Any
) -> None:
    """Write `changes` into the failure's payload when any differs from what it holds."""
    # Stored as JSON: compare them as the next retry reads them.
    changes = json.loads(json.dumps(changes))
    if any(changes[k] != failure.payload.get(k) for k in changes):
        await _update_payload(failure, changes, ch=ch)


async def _update_payload(
    failure: ProjectionFailureRow,
    changes: dict[str, Any],
    *,
    drop: tuple[str, ...] = (),
    ch: Any,
) -> None:
    """Write `changes` into the failure's payload, without the keys in `drop`."""
    payload = {k: v for k, v in failure.payload.items() if k not in drop}
    payload.update(changes)
    # Synchronous: the next retry reads the payload this one leaves.
    await ch.execute(
        """
        ALTER TABLE loom.memory_projection_failures
        UPDATE payload = {payload:String}
        WHERE org = {org:String}
          AND toString(failure_id) = {failure_id:String}
          AND status = 'active'
        SETTINGS mutations_sync = 2
        """,
        params={
            "org": failure.org,
            "failure_id": failure.failure_id,
            "payload": json.dumps(payload, sort_keys=True, default=str),
        },
    )
