"""Metadata-only source refresh leases under the Postgres knowledge authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from asyncpg import Connection

from server.core import pg
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.domain.mcp_worker_refresh import (
    CompletionReceipt,
    DocumentTask,
    LiveLease,
    Observation,
    ObservationKind,
    RefreshRejected,
    RefreshScope,
)
from server.knowledge.store import pg_worker_refresh_admission as admission

_MAX_ATTEMPTS = 8
_KEY = "org = $1 AND namespace = $2 AND item_key = $3"
_ENQUEUE = """
INSERT INTO loom.knowledge_source_refresh (
    org, namespace, item_key, worker_id, source_uri, state, claimable_at,
    agent, user_id, session_id, request_sha256, task, actor, source_class, admission_source_id
) VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10, $11::jsonb, $12, 'external', $13)
ON CONFLICT (org, namespace, item_key) DO NOTHING
RETURNING 1
"""
_FINGERPRINT = f"SELECT request_sha256 FROM loom.knowledge_source_refresh WHERE {_KEY}"
_CLAIM = """
WITH ready AS (
    SELECT org, namespace, item_key FROM loom.knowledge_source_refresh
    WHERE org = $1 AND namespace = $2 AND worker_id = $3
      AND state IN ('pending', 'claimed')
      AND claimable_at <= statement_timestamp()
    ORDER BY claimable_at, item_key
    LIMIT $4 FOR UPDATE SKIP LOCKED
), taken AS (
    UPDATE loom.knowledge_source_refresh AS job
    SET state = CASE WHEN job.attempts >= $5 THEN 'exhausted' ELSE 'claimed' END,
        attempts = LEAST(job.attempts + 1, $5::smallint),
        fence = CASE WHEN job.attempts < $5 THEN nextval('loom.knowledge_source_refresh_fence')
                     ELSE job.fence END,
        claimable_at = clock_timestamp() + make_interval(secs => $6),
        updated_at = clock_timestamp()
    FROM ready WHERE (job.org, job.namespace, job.item_key)
                  = (ready.org, ready.namespace, ready.item_key)
    RETURNING job.item_key, job.fence, job.claimable_at, job.task, job.state
)
SELECT item_key, fence, claimable_at, task, state FROM taken
"""
_OWNS = f"""
{_KEY} AND worker_id = $4 AND fence = $5
AND state = 'claimed' AND claimable_at = $6
"""
_LOCK = f"""
WITH locked AS MATERIALIZED (
    SELECT task, claimable_at FROM loom.knowledge_source_refresh WHERE {_OWNS} FOR UPDATE
)
SELECT task, claimable_at FROM locked WHERE claimable_at > clock_timestamp()
"""
_CHECKPOINT = f"""
WITH locked AS MATERIALIZED (
    SELECT claimable_at AS deadline FROM loom.knowledge_source_refresh WHERE {_OWNS} FOR UPDATE
)
UPDATE loom.knowledge_source_refresh
SET state = 'completed', updated_at = clock_timestamp(), receipt = $7::jsonb
FROM locked WHERE {_OWNS} AND locked.deadline > clock_timestamp() RETURNING 1
"""
_CANCEL = f"""
UPDATE loom.knowledge_source_refresh
SET state = 'cancelled', updated_at = clock_timestamp()
WHERE {_KEY} AND user_id = $4 AND state IN ('pending', 'claimed')
RETURNING 1
"""
_TERMINAL = f"""
SELECT task, receipt, user_id, claimable_at, updated_at FROM loom.knowledge_source_refresh
WHERE {_KEY} AND worker_id = $4 AND fence = $5 AND claimable_at = $6 AND state = 'completed'
"""
_LOCKED_TERMINAL = f"""
WITH locked AS MATERIALIZED ({_TERMINAL} FOR SHARE)
SELECT * FROM locked WHERE updated_at > clock_timestamp() - interval '24 hours'
"""


@dataclass(frozen=True, repr=False)
class RefreshEntry:
    task: DocumentTask
    worker_id: UUID
    user_id: str
    due: datetime
    agent: str
    session_id: str


@dataclass(frozen=True, repr=False)
class CompletionAuthority:
    """Current owner and worker for the exact task, including its copy destination."""

    task: DocumentTask
    worker_id: str
    user_id: str


@dataclass(frozen=True, repr=False)
class WorkerCompletion:
    lease: LiveLease
    receipt: CompletionReceipt
    authorize: Callable[[Connection, LiveLease], Awaitable[CompletionAuthority | None]]


def _metadata(entry: RefreshEntry) -> dict[str, Any]:
    if (
        entry.worker_id.int == 0
        or not entry.user_id
        or entry.user_id == "-"
        or len(entry.user_id.encode("utf-8")) > 255
        or entry.due.utcoffset() is None
    ):
        raise RefreshRejected("invalid refresh queue metadata")
    return {"version": 1, "task": asdict(entry.task)}


def _task(metadata: dict[str, Any]) -> DocumentTask:
    try:
        if metadata["version"] != 1:
            raise RefreshRejected("unsupported refresh queue metadata")
        item = metadata["task"]
        return DocumentTask(item["job_id"], RefreshScope(**item["scope"]), item["resource_id"])
    except (KeyError, TypeError, ValueError, RecursionError):
        raise RefreshRejected("invalid refresh queue metadata") from None


def _require_transaction(conn: Connection) -> None:
    if not pg.enabled() or not conn.is_in_transaction():
        raise RefreshRejected("source refresh requires Postgres authority transaction")


async def enqueue_batch_on(conn: Connection, batch: tuple[RefreshEntry, ...]) -> int:
    """Admit an exact batch from current source authority, or leave it pending."""
    _require_transaction(conn)
    if not batch:
        raise RefreshRejected("source refresh batch is empty")
    first = batch[0]
    task = first.task
    scope = task.scope
    source_id = UUID(scope.source_id)
    if str(source_id) != scope.source_id or source_id.int == 0:
        raise RefreshRejected("invalid refresh source identity")
    if any(entry != _replace_task(first, entry.task) for entry in batch) or len(
        {entry.task.resource_id for entry in batch}
    ) != len(batch):
        raise RefreshRejected("refresh batch identity changed")
    source = admission.AdmissionSource(
        scope.org,
        scope.namespace,
        source_id,
        first.agent,
        first.user_id,
        first.session_id,
        scope.actor,
    )
    async with conn.transaction():
        if not await admission.reserve_on(
            conn, source, admission.QueueAssignment(first.worker_id, scope), len(batch)
        ):
            return 0
        for entry in batch:
            if not await _enqueue_on(conn, entry, source_id):
                raise RefreshRejected("refresh batch was already admitted")
    return len(batch)


def _replace_task(first: RefreshEntry, task: DocumentTask) -> RefreshEntry:
    if task.scope != first.task.scope or task.job_id != first.task.job_id:
        raise RefreshRejected("refresh batch task changed")
    return replace(first, task=task)


async def _enqueue_on(conn: Connection, entry: RefreshEntry, source_id: UUID) -> bool:
    scope = entry.task.scope
    source_uri = resource_uri(scope.source_id, entry.task.resource_id)
    metadata = _metadata(entry)
    key = (scope.org, scope.namespace, entry.task.idempotency_key)
    digest = hashlib.sha256(
        json.dumps(
            [
                metadata,
                str(entry.worker_id),
                source_uri,
                entry.due.astimezone(UTC).isoformat(),
                entry.agent,
                entry.user_id,
                entry.session_id,
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if (
        await conn.fetchval(
            _ENQUEUE,
            *key,
            entry.worker_id,
            source_uri,
            entry.due,
            entry.agent,
            entry.user_id,
            entry.session_id,
            digest,
            metadata,
            scope.actor,
            source_id,
        )
        is not None
    ):
        return True
    if await conn.fetchval(_FINGERPRINT, *key) != digest:
        raise RefreshRejected("refresh request changed")
    return False


def _claim_limit(value: int, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


@dataclass(frozen=True, slots=True, repr=False)
class ClaimPage:
    leases: tuple[LiveLease, ...]
    retired: int


async def claim(
    *,
    org: str,
    namespace: str,
    worker_id: UUID,
    limit: int,
    lease_seconds: int,
) -> ClaimPage:
    """Process one bounded worker page, distinguishing exhaustion from an idle poll."""
    if not pg.enabled():
        raise RefreshRejected("source refresh requires Postgres authority")
    if not (worker_id.int and _claim_limit(limit, 64) and _claim_limit(lease_seconds, 300)):
        raise RefreshRejected("invalid refresh claim limits")
    async with pg.transaction() as conn:
        claimed = await conn.fetch(
            _CLAIM, org, namespace, worker_id, limit, _MAX_ATTEMPTS, lease_seconds
        )
        leases = []
        retired = 0
        for row in claimed:
            if row["state"] == "exhausted":
                retired += 1
                continue
            task = _task(row["task"])
            if (task.scope.org, task.scope.namespace, task.idempotency_key) != (
                org,
                namespace,
                row["item_key"],
            ):
                raise RefreshRejected("refresh claim scope mismatch")
            leases.append(LiveLease(task, str(worker_id), int(row["fence"]), row["claimable_at"]))
        return ClaimPage(tuple(leases), retired)


def _lease_params(lease: LiveLease) -> tuple[Any, ...]:
    return (
        lease.task.scope.org,
        lease.task.scope.namespace,
        lease.task.idempotency_key,
        UUID(lease.worker_id),
        lease.fence,
        lease.expires_at,
    )


async def lock_current(conn: Connection, lease: LiveLease) -> LiveLease:
    """Lock the exact lease in the transaction that commits the document head."""
    _require_transaction(conn)
    result = await conn.fetchrow(_LOCK, *_lease_params(lease))
    if result is None or _task(result["task"]) != lease.task:
        raise RefreshRejected("refresh lease is no longer current")
    return LiveLease(lease.task, lease.worker_id, lease.fence, result["claimable_at"])


async def checkpoint(conn: Connection, lease: LiveLease, receipt: CompletionReceipt) -> None:
    """Recheck the database clock and settle only inside the document transaction."""
    _require_transaction(conn)
    if (
        receipt.idempotency_key != lease.task.idempotency_key
        or receipt.observation.kind is not ObservationKind.PRESENT
    ):
        raise RefreshRejected("receipt does not authorize an accepted copy")
    if await conn.fetchval(_CHECKPOINT, *_lease_params(lease), asdict(receipt)) is None:
        raise RefreshRejected("refresh lease is no longer current")


async def cancel(conn: Connection, task: DocumentTask, *, user_id: str) -> bool:
    """The authorized owner's stop fences unfinished work without touching its copies."""
    _require_transaction(conn)
    if not user_id or user_id == "-":
        raise RefreshRejected("source refresh cancellation unavailable")
    scope = task.scope
    return (
        await conn.fetchval(_CANCEL, scope.org, scope.namespace, task.idempotency_key, user_id)
        is not None
    )


async def replay_completion(conn: Connection, completion: WorkerCompletion) -> bool:
    """An exact terminal retry reads a receipt after fresh authority, without writing a copy."""
    _require_transaction(conn)
    lease = completion.lease
    scope = lease.task.scope
    await admission.lock_namespace_on(conn, scope.org, scope.namespace)
    row = await conn.fetchrow(_TERMINAL, *_lease_params(lease))
    if row is None or _task(row["task"]) != lease.task:
        return False
    authority = await completion.authorize(
        conn, LiveLease(lease.task, lease.worker_id, lease.fence, row["claimable_at"])
    )
    if authority is None or (authority.task, authority.worker_id, authority.user_id) != (
        lease.task,
        lease.worker_id,
        row["user_id"],
    ):
        return False
    # Lock in source-before-queue order, then recheck expiry and the exact row.
    locked = await conn.fetchrow(_LOCKED_TERMINAL, *_lease_params(lease))
    if locked is None or locked != row:
        return False
    try:
        raw = locked["receipt"]
        receipt = CompletionReceipt(
            raw["idempotency_key"],
            Observation(
                ObservationKind(raw["observation"]["kind"]), raw["observation"]["content_sha256"]
            ),
        )
    except (ValueError, KeyError, TypeError, RecursionError):
        raise RefreshRejected("source refresh receipt unavailable") from None
    if completion.receipt != receipt:
        raise RefreshRejected("source refresh receipt conflicts")
    return True
