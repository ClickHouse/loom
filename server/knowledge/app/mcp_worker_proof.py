"""Confirm current upstream access to the exact bytes of a stored source copy."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable

from server.knowledge.app.mcp_worker_authority import CurrentRead
from server.knowledge.app.mcp_worker_read import WorkerReadChannel, WorkerReadRefused
from server.knowledge.domain.mcp_worker_permissions import (
    ReaderConnection,
    StoredCopyRevision,
    VerifiedWorkerReadEvidence,
    can_read_stored_copy,
)


def _epoch_seconds() -> int:
    return int(time.time())


async def confirm_stored_read(
    *,
    channel: WorkerReadChannel,
    current: CurrentRead,
    revision: Callable[[], Awaitable[StoredCopyRevision | None]],
    max_age_seconds: int,
    now: Callable[[], int] = _epoch_seconds,
) -> VerifiedWorkerReadEvidence:
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300:
        raise ValueError("invalid upstream access evidence lifetime")
    if current.purpose != "stored":
        raise WorkerReadRefused("denied")
    scope = await current.resolve()
    accepted = await revision()
    resource_digest = hashlib.sha256(scope.resource_id.encode()).hexdigest()
    if (
        accepted is None
        or (accepted.org, accepted.namespace) != (scope.org, scope.namespace)
        or accepted.source_id != scope.source_id
        or accepted.selection_generation != scope.selection_generation
        or accepted.resource_id_sha256 != resource_digest
    ):
        raise WorkerReadRefused("denied")
    started = now()
    reading = await channel.run(scope, "check_access", require_current=current.resolve)
    if reading.content_sha256 != accepted.content_sha256 or await revision() != accepted:
        raise WorkerReadRefused("denied")
    reader = ReaderConnection(
        scope.requester,
        scope.worker_id,
        scope.connection_id,
        scope.connection_generation,
        scope.profile_digest,
    )
    proof = VerifiedWorkerReadEvidence(
        confirmed_revision=accepted,
        connection=reader,
        issued_at=started,
        confirmed_at=reading.fetched_at,
        expires_at=started + max_age_seconds,
    )
    if await current.resolve() != scope or not can_read_stored_copy(
        collection_granted=True,
        requester=scope.requester,
        current_revision=accepted,
        active_connection=reader,
        evidence=proof,
        now=now(),
        max_evidence_age_seconds=max_age_seconds,
    ):
        raise WorkerReadRefused("denied")
    return proof
