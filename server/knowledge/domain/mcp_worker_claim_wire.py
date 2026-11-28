"""Project authorized refresh leases into metadata for the assigned worker."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypedDict
from uuid import UUID

from server.knowledge.domain.mcp_worker_refresh import (
    DocumentTask,
    LiveLease,
    RefreshRejected,
    RefreshScope,
    WriteAuthority,
    validate_lease,
)
from server.knowledge.domain.mcp_worker_upload import CurrentRawCopy, RawVersion

_REFUSED = "refresh claim refused"


@dataclass(frozen=True, slots=True, repr=False)
class ClaimWorkerBinding:
    """Trusted worker enrollment for the current connection and its owner."""

    worker_id: str
    org: str
    owner: str
    connection_id: str
    connection_generation: int
    profile_digest: str


class _ScopeWire(TypedDict):
    org: str
    namespace: str
    source_id: str
    connection_id: str
    destination_id: str
    connection_generation: str
    selection_generation: str
    approval_generation: str
    profile_digest: str


class _TaskWire(TypedDict):
    job_id: str
    scope: _ScopeWire
    resource_id: str


class _PreviousWire(TypedDict):
    body_sha256: str
    revision: str | None


class RefreshClaimWire(TypedDict):
    version: Literal[1]
    task: _TaskWire
    worker_id: str
    requester: str
    source_generation: str
    fence: int
    expires_at: str
    idempotency_key: str
    previous: _PreviousWire | None


def _canonical_uuid(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.int != 0


def project_refresh_claim(
    lease: LiveLease | None,
    *,
    authority: WriteAuthority | None,
    owner: str | None,
    worker: ClaimWorkerBinding,
    now: datetime,
    current: CurrentRawCopy | None = None,
) -> RefreshClaimWire:
    """Project current store state; no argument may come from the claim request.

    Resolve authority, owner and any current copy for this exact task independently
    of the lease. The response grants no write authority and does not replace a
    fresh check at revalidation or settlement.
    """
    if (
        not isinstance(lease, LiveLease)
        or not isinstance(lease.task, DocumentTask)
        or not isinstance(lease.task.scope, RefreshScope)
        or not isinstance(authority, WriteAuthority)
        or not isinstance(worker, ClaimWorkerBinding)
        or type(worker.connection_generation) is not int
        or not isinstance(owner, str)
        or not isinstance(now, datetime)
        or not isinstance(lease.expires_at, datetime)
        or (current is not None and not isinstance(current, CurrentRawCopy))
    ):
        raise RefreshRejected(_REFUSED)
    task = lease.task
    scope = task.scope
    if (
        worker
        != ClaimWorkerBinding(
            lease.worker_id,
            scope.org,
            owner,
            scope.connection_id,
            scope.connection_generation,
            scope.profile_digest,
        )
        or not _canonical_uuid(worker.worker_id)
        or not _canonical_uuid(scope.source_id)
        or not _canonical_uuid(scope.connection_id)
        or max(len(scope.org), len(scope.namespace), len(owner)) > 255
        or not owner.isprintable()
        or owner in {"", "-"}
        or owner != owner.strip()
    ):
        raise RefreshRejected(_REFUSED)
    try:
        validate_lease(
            task,
            worker_id=worker.worker_id,
            fence=lease.fence,
            lease=lease,
            authority=authority,
            now=now,
        )
        expires_at = lease.expires_at.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        raise RefreshRejected(_REFUSED) from None

    previous: _PreviousWire | None = None
    if current is not None and current.scope == scope and current.resource_id == task.resource_id:
        if not isinstance(current.version, RawVersion):
            raise RefreshRejected(_REFUSED)
        previous = {"body_sha256": current.version.sha256, "revision": current.version.revision}
    return {
        "version": 1,
        "task": {
            "job_id": task.job_id,
            "scope": {
                "org": scope.org,
                "namespace": scope.namespace,
                "source_id": scope.source_id,
                "connection_id": scope.connection_id,
                "destination_id": scope.destination_id,
                "connection_generation": str(scope.connection_generation),
                "selection_generation": str(scope.selection_generation),
                "approval_generation": str(scope.approval_generation),
                "profile_digest": scope.profile_digest,
            },
            "resource_id": task.resource_id,
        },
        "worker_id": worker.worker_id,
        "requester": owner,
        "source_generation": str(scope.source_generation),
        "fence": lease.fence,
        "expires_at": expires_at.removesuffix("+00:00") + "Z",
        "idempotency_key": task.idempotency_key,
        "previous": previous,
    }
