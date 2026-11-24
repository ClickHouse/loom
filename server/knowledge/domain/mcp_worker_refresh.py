"""Metadata-only contract for a bounded batch of selected MCP document reads.

Periodic scheduling and Sync now produce the same job. A job authorizes no reads
or writes on its own. At publication, the caller must obtain the live lease, clock
and current authority from Loom, not the completion request, and make validation
and the guarded write indivisible with respect to changes in those inputs.
"""

import hashlib
import json
import re
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from server.knowledge.domain.mcp_identity import resource_digest

MAX_DOCUMENTS_PER_JOB = 64
MAX_IDENTIFIER_BYTES = 1024
_MAX_FENCE = (1 << 32) - 1
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RefreshRejected(ValueError):
    """The refresh metadata, lease, or proposed completion cannot be used."""


def _require_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > MAX_IDENTIFIER_BYTES
        or not value.isprintable()
        or not value.strip()
        or len(value.encode()) > MAX_IDENTIFIER_BYTES
    ):
        raise RefreshRejected("invalid refresh identifier")


def _require_resource(value: str) -> None:
    _require_identifier(value)
    try:
        resource_digest(value)
    except ValueError:
        raise RefreshRejected("invalid refresh resource") from None


@dataclass(frozen=True, slots=True)
class RefreshScope:
    """Loom's canonical actor and stable source, connection and destination IDs."""

    org: str
    namespace: str
    actor: str
    source_id: str
    connection_id: str
    destination_id: str
    connection_generation: int
    source_generation: int
    selection_generation: int
    approval_generation: int
    profile_digest: str

    def __post_init__(self) -> None:
        for identifier in (
            self.org,
            self.namespace,
            self.actor,
            self.source_id,
            self.connection_id,
            self.destination_id,
        ):
            _require_identifier(identifier)
        for generation in (
            self.connection_generation,
            self.source_generation,
            self.selection_generation,
            self.approval_generation,
        ):
            if type(generation) is not int or not 0 < generation < 1 << 63:
                raise RefreshRejected("invalid refresh generation")
        if not isinstance(self.profile_digest, str) or not _SHA256.fullmatch(self.profile_digest):
            raise RefreshRejected("invalid refresh profile")


@dataclass(frozen=True, slots=True)
class DocumentTask:
    job_id: str
    scope: RefreshScope
    resource_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.job_id)
        _require_resource(self.resource_id)

    @property
    def idempotency_key(self) -> str:
        """One key per selected document in a job, unchanged by reclaims."""
        data = json.dumps(("mcp-refresh-v1", *astuple(self)), separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RefreshJob:
    job_id: str
    scope: RefreshScope
    resources: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.job_id)
        if (
            not isinstance(self.resources, tuple)
            or not 0 < len(self.resources) <= MAX_DOCUMENTS_PER_JOB
        ):
            raise RefreshRejected("invalid refresh job size")
        for resource_id in self.resources:
            _require_resource(resource_id)
        if len(set(self.resources)) != len(self.resources):
            raise RefreshRejected("duplicate resource in refresh job")

    def document_tasks(self) -> tuple[DocumentTask, ...]:
        return tuple(DocumentTask(self.job_id, self.scope, resource) for resource in self.resources)


@dataclass(frozen=True, slots=True)
class LiveLease:
    """Current coordination-store lease, not a historical claim or task projection."""

    task: DocumentTask
    worker_id: str
    fence: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WriteAuthority:
    """Current job membership, selection, actor delegation and worker authorization."""

    task: DocumentTask
    worker_id: str


class ObservationKind(StrEnum):
    PRESENT = "present"
    TIMEOUT = "timeout"
    OMITTED = "omitted"
    INCOMPLETE = "incomplete"
    AMBIGUOUS_ABSENCE = "ambiguous_absence"


@dataclass(frozen=True, slots=True)
class Observation:
    """Normalized observation; Loom hashes the validated ingest payload for a present item."""

    kind: ObservationKind
    content_sha256: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ObservationKind):
            raise RefreshRejected("unknown refresh observation")
        if self.kind is ObservationKind.PRESENT:
            if not isinstance(self.content_sha256, str) or not _SHA256.fullmatch(
                self.content_sha256
            ):
                raise RefreshRejected("present document requires a SHA-256 digest")
        elif self.content_sha256 != "":
            raise RefreshRejected("only a present document carries a digest")


@dataclass(frozen=True, slots=True)
class Completion:
    task: DocumentTask
    fence: int
    idempotency_key: str
    observation: Observation


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    """Accepted observation and per-document checkpoint, written only with its effect."""

    idempotency_key: str
    observation: Observation


class CompletionAction(StrEnum):
    UPSERT = "upsert"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    action: CompletionAction
    receipt_to_commit: CompletionReceipt | None = None


def validate_lease(
    task: DocumentTask,
    *,
    worker_id: str,
    fence: int,
    lease: LiveLease | None,
    authority: WriteAuthority | None,
    now: datetime,
) -> WriteAuthority:
    """Validate a positive current fence and a deadline strictly after Loom's clock."""
    _require_identifier(worker_id)
    if (
        lease is None
        or task != lease.task
        or worker_id != lease.worker_id
        or type(fence) is not int
        or type(lease.fence) is not int
        or not 0 < fence <= _MAX_FENCE
        or fence != lease.fence
    ):
        raise RefreshRejected("refresh lease does not match")
    if (
        now.utcoffset() is None
        or lease.expires_at.utcoffset() is None
        or now.astimezone(UTC) >= lease.expires_at.astimezone(UTC)
    ):
        raise RefreshRejected("refresh lease deadline reached or invalid")
    if authority is None or authority.task != task or authority.worker_id != worker_id:
        raise RefreshRejected("refresh authority does not match")
    return authority


def validate_completion(
    completion: Completion,
    *,
    worker_id: str,
    lease: LiveLease | None,
    authority: WriteAuthority | None,
    now: datetime,
) -> CompletionDecision:
    """Propose an upsert under a live lease. Only the store may replay terminal work."""
    validate_lease(
        completion.task,
        worker_id=worker_id,
        fence=completion.fence,
        lease=lease,
        authority=authority,
        now=now,
    )
    key = completion.task.idempotency_key
    if completion.idempotency_key != key:
        raise RefreshRejected("refresh idempotency key does not match")
    if completion.observation.kind is ObservationKind.PRESENT:
        receipt = CompletionReceipt(key, completion.observation)
        return CompletionDecision(CompletionAction.UPSERT, receipt)
    return CompletionDecision(CompletionAction.RETRY)
