"""The rows the erasure store reads and writes, as the columns its statements
name. They stay dicts at run time: a checkpoint is the ledger row copied
forward with new columns, and a census or export row is serialized as read."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TypedDict
from uuid import UUID


class MutationRow(TypedDict):
    """A saga's own command in system.mutations."""

    mutation_id: str
    is_done: int
    parts_to_do: int
    latest_fail_reason: str


class RequestRow(TypedDict):
    """The columns every loom.erasure_requests row of one saga repeats."""

    id: str | UUID
    org: str
    scope_type: str
    scope_value: str
    requester: str
    actor: str
    urgency: str
    purge: bool
    attempts: int
    requested_at: datetime


class VerifyColumns(TypedDict, total=False):
    """The residue census on a terminal ledger row."""

    residue_count: int
    residue: str
    verified_at: datetime | None


class CheckpointRow(RequestRow, VerifyColumns):
    """One state transition of a saga, as loom.erasure_requests stores it."""

    status: str
    current_step: int
    failure_reason: str
    completed_at: datetime | None
    steps_completed: list[str]
    last_checkpoint_at: datetime
    version: int


class ResumeRow(RequestRow):
    """A saga the recovery worker replays: its request and its last checkpoint."""

    steps_completed: list[str]
    failure_reason: str
    residue: str


class LedgerRow(ResumeRow):
    """A saga with its last residue census, for an abandonment or a re-verify."""

    residue_count: int
    verified_at: datetime | None


class RequestStatusRow(TypedDict):
    """The current state of one saga, for `erasure.status`."""

    id: str | UUID
    org: str
    scope_type: str
    scope_value: str
    requester: str
    status: str
    current_step: int
    failure_reason: str
    requested_at: datetime
    completed_at: datetime | None
    residue_count: int
    verified_at: datetime | None
    residue: str


class OrphanGraphRow(TypedDict):
    """A namespace whose entity graph outlived everything that wrote it."""

    org: str
    namespace: str
    edges: int


class MemoryExportRow(TypedDict):
    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_id: str
    memory_key: str
    memory_type: str
    value: str
    confidence: float
    source_trace: str
    version: int
    tombstoned_at: datetime | None
    created_at: datetime
    expires_at: datetime | None
    sensitivity: str


class ChunkExportRow(TypedDict):
    org: str
    namespace: str
    source_uri: str
    chunk_idx: int
    content: str
    context: str
    tags: list[str]
    doc_metadata: str
    chunk_metadata: str
    ingested_at: datetime
    doc_superseded_at: datetime | None
    embedding_model: str
    embedding_dim: int


class ConversationExportRow(TypedDict):
    org: str
    namespace: str
    agent: str
    user_id: str
    thread_id: str
    msg_id: str
    role: str
    content: str
    reasoning: str
    metadata: str
    tokens: int
    run_id: str
    ts: datetime
    is_saved: bool
    saved_at: datetime | None
    tombstoned_at: datetime | None


class ConversationThreadExportRow(TypedDict):
    org: str
    namespace: str
    agent: str
    user_id: str
    thread_id: str
    title: str
    metadata: str
    run_id: str
    created_at: datetime
    updated_at: datetime
    tombstoned_at: datetime | None


class AgentEventExportRow(TypedDict):
    ts: datetime
    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    operation: str
    request_id: str
    trace_id: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    embedding_tokens: int
    upstream_cost_usd: Decimal
    bytes_written: int
    success: bool
    error_code: str
    kind: str
    args: dict[str, str]


class AuditExportRow(TypedDict):
    ts: datetime
    event_type: str
    org: str
    namespace: str
    actor: str
    subject: str
    request_id: str
