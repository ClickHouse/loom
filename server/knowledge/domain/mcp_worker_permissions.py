"""Read policy for stored MCP copies, separate from worker I/O and signature verification."""

import re
from dataclasses import dataclass
from uuid import UUID

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, repr=False)
class StoredCopyRevision:
    org: str
    namespace: str
    source_id: UUID
    selection_generation: int
    resource_id_sha256: str
    revision_id: UUID
    content_sha256: str


@dataclass(frozen=True, repr=False)
class ReaderConnection:
    owner: str
    worker_id: UUID
    connection_id: UUID
    connection_generation: int
    profile_digest: str


@dataclass(frozen=True, repr=False)
class VerifiedWorkerReadEvidence:
    """Internal receipt minted by core, never decoded from public request data.

    Core authenticates the approved worker's response and binds it to the
    read challenge for this requester, connection, and stored revision. The
    content digest comes from the confirmed result, not from the challenge.
    """

    confirmed_revision: StoredCopyRevision
    connection: ReaderConnection
    issued_at: int
    confirmed_at: int
    expires_at: int


def _known_identity(value: str) -> bool:
    return isinstance(value, str) and value not in ("", "-") and value == value.strip()


def _known_uuid(value: UUID) -> bool:
    return isinstance(value, UUID) and value.int != 0


def _positive_int(value: int) -> bool:
    return type(value) is int and value > 0


def _known_digest(value: str) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def can_read_stored_copy(
    *,
    collection_granted: bool | None,
    requester: str | None,
    current_revision: StoredCopyRevision | None,
    active_connection: ReaderConnection | None,
    evidence: VerifiedWorkerReadEvidence | None,
    now: int,
    max_evidence_age_seconds: int,
) -> bool:
    """Authorize only the current copy and only for the authenticated requester.

    Core supplies current authority: a selected and readable revision, the
    requester's own approved active connection for the source, and evidence
    not superseded by a denial or revocation. Unknown state is passed as None.
    Times are UTC epoch seconds. False must exclude the copy, not change its mode.
    """
    if (
        collection_granted is not True
        or requester is None
        or not _known_identity(requester)
        or current_revision is None
        or active_connection is None
        or not isinstance(evidence, VerifiedWorkerReadEvidence)
        or active_connection.owner != requester
        or current_revision != evidence.confirmed_revision
        or active_connection != evidence.connection
    ):
        return False

    if not (
        _known_identity(current_revision.org)
        and _known_identity(current_revision.namespace)
        and _known_uuid(current_revision.source_id)
        and _positive_int(current_revision.selection_generation)
        and _known_digest(current_revision.resource_id_sha256)
        and _known_uuid(current_revision.revision_id)
        and _known_digest(current_revision.content_sha256)
        and _known_uuid(active_connection.worker_id)
        and _known_uuid(active_connection.connection_id)
        and _positive_int(active_connection.connection_generation)
        and _known_digest(active_connection.profile_digest)
    ):
        return False

    return (
        _positive_int(max_evidence_age_seconds)
        and _positive_int(now)
        and _positive_int(evidence.issued_at)
        and _positive_int(evidence.confirmed_at)
        and _positive_int(evidence.expires_at)
        and evidence.issued_at <= evidence.confirmed_at <= now < evidence.expires_at
        and evidence.expires_at - evidence.issued_at <= max_evidence_age_seconds
    )
