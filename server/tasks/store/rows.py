"""The rows the task inbox statements read and write.

TypedDicts, not classes: a row read here is copied forward into the next
version row column for column (the ReplacingMergeTree rule), so it stays the
dict the driver returns. ``task_id`` and ``parent_task_id`` are UUID columns:
the driver reads them as ``uuid.UUID``, the handlers write them as ``str``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict
from uuid import UUID

from server.tasks.domain.states import Disclosure, PolicyMode, RunStatus, TaskStatus


class InboxRow(TypedDict):
    """One ``loom.task_inboxes`` version (``INBOX_COLS``)."""

    org: str
    inbox: str
    owner_agent: str
    owner_namespace: str
    description: str
    posters: list[str]
    accept_disclosure: list[str]
    default_lease_seconds: int
    max_attempts: int
    max_defers: int
    triage_escalate_after_seconds: int
    webhook_url: str
    webhook_secret_ref: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    version: int
    is_deleted: int


class PolicyRow(TypedDict):
    """One ``loom.task_poster_policies`` version (``POLICY_COLS``)."""

    org: str
    inbox: str
    poster: str
    kind: str
    mode: PolicyMode
    daily_cap: int
    require_evidence: list[str]
    promote_after: int
    require_human: int
    streak: int
    set_by: str
    set_at: datetime
    version: int
    is_deleted: int


class TaskRow(TypedDict, total=False):
    """One ``loom.tasks`` version (``TASK_COLS``). Not total: a list read
    projects the columns its caller asked for."""

    org: str
    inbox: str
    namespace: str
    task_id: str | UUID
    kind: str
    priority: int
    disclosure: Disclosure
    status: TaskStatus
    reason: str
    blocked_on: str
    not_before: datetime | None
    resume_at: datetime | None
    deadline: datetime | None
    defer_count: int
    title: str
    brief: str
    from_agent: str
    from_subject: str
    origin: str
    parent_task_id: str | UUID | None
    idempotency_key: str
    owner: str
    attempt: int
    fence: int
    lease_until: datetime | None
    outcome: str
    last_actor: str
    created_at: datetime
    updated_at: datetime
    version: int
    is_deleted: int


class TaskEventRow(TypedDict):
    """One ``loom.task_events`` row (``EVENT_COLS``)."""

    org: str
    inbox: str
    namespace: str
    task_id: str | UUID
    seq: int
    event: str
    from_status: str
    to_status: str
    actor: str
    agent: str
    from_subject: str
    fence: int
    version: int
    reason: str
    patch: str
    note: str
    ts: datetime


class StaleTriageRow(TypedDict):
    """A triage task past its inbox's escalation threshold."""

    org: str
    inbox: str
    namespace: str
    task_id: str | UUID
    from_subject: str
    created_at: datetime
    after_s: int


class SweepCandidates(TypedDict):
    """What one sweep acts on."""

    expired: list[TaskRow]
    resume: list[TaskRow]
    stale_triage: list[StaleTriageRow]


class InboxCountRow(TypedDict):
    """Tasks per (inbox, status, blocked_on) and the oldest one's age."""

    inbox: str
    status: str
    blocked_on: str
    n: int
    oldest: datetime


class ServiceAccountRow(TypedDict):
    name: str
    sa_subject: str
    namespaces: list[str]
    roles: list[str]
    ns_roles: str
    kind: str
    client: str
    created_by: str
    created_at: datetime


class GrantSubjectRow(TypedDict):
    subject: str
    namespace: str
    role: str


class WebhookInboxRow(TypedDict):
    """An inbox the delivery worker posts to."""

    org: str
    inbox: str
    owner_namespace: str
    webhook_url: str
    webhook_secret_ref: str


class WebhookCursorRow(TypedDict):
    """One ``loom.task_webhook_cursor`` version (``WEBHOOK_CURSOR_COLS``)."""

    org: str
    inbox: str
    last_seq: int
    attempts: int
    next_try_at: datetime
    last_status: int
    version: int


class RunLedgerRow(TypedDict, total=False):
    """One ``loom.agent_runs`` version. Not total: a row read from a replica
    behind the grade columns (or a test fake) lacks them."""

    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    run_id: str
    goal: str
    user_request: str
    expected_outcome: str
    environment: str
    model: str
    consumer: str
    status: RunStatus
    started_at: datetime
    completed_at: datetime | None
    duration_ms: int
    final_response: str
    confidence: float
    memories_used: list[str]
    memories_written: list[str]
    metadata: str
    grade: str
    grade_source: str
    grade_reason: str
    grade_ref: str
    graded_at: datetime | None
    # Set only by a session run's first begin; every other write takes the server's clock.
    version: int
