"""The task inbox plane — `task.*` and `inbox.*`.

One inbox per agent, and the inbox is its queue. A poster the inbox does not
yet trust for a kind lands in ``triage``; accepted work waits in ``todo``; a
pull claims under a lease with a fence token; every move off the happy path
carries a reason. Agents push their writes and pull their work: ``task.pull``
and ``task.updates`` long-poll, nothing here pushes a task to an agent.

Every input carries the caller's own ``(org, namespace, agent)`` — the
namespace the call is made under, which the transport authorizes as usual —
and names the inbox or the task it addresses; the handler then authorizes
against the inbox's owner namespace or the stored row (a refusal on an
id-addressed read answers ``found = false``, never a 403 that confirms the
id exists).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from server.core.webhook_secrets import (
    WEBHOOK_SECRET_PREFIX,
    webhook_secret_org_prefix,
    webhook_secret_ref_ok,
)
from server.tasks.domain.states import BlockedOn, Disclosure, PolicyMode, TaskStatus

# The long-poll ceiling: a pull or an updates read may hold this long.
MAX_WAIT_SECONDS = 30


def _all_disclosures() -> list[Disclosure]:
    return ["confidential", "internal", "public_ok"]


class _Caller(BaseModel):
    """The identity every call is made under (the transport authorizes the
    caller on ``namespace``); ``agent`` names the acting agent — the claimant
    of a pull, the poster of a create."""

    org: str
    namespace: str
    agent: str = "-"


# ============================================================
# payloads
# ============================================================


class TaskPayload(BaseModel):
    task_id: str
    org: str
    inbox: str
    namespace: str
    kind: str
    priority: int
    disclosure: Disclosure
    status: TaskStatus
    reason: str = ""
    blocked_on: str = ""
    not_before: datetime | None = None
    resume_at: datetime | None = None
    deadline: datetime | None = None
    defer_count: int = 0
    title: str
    brief: dict[str, Any] = Field(default_factory=dict)
    from_agent: str
    from_subject: str = ""
    origin: str = ""
    parent_task_id: str | None = None
    owner: str = ""
    attempt: int = 0
    fence: int = 0
    lease_until: datetime | None = None
    outcome: dict[str, Any] = Field(default_factory=dict)
    last_actor: str = ""
    created_at: datetime
    updated_at: datetime
    version: int


class TaskEventPayload(BaseModel):
    seq: int
    task_id: str
    inbox: str
    event: str
    from_status: str = ""
    to_status: str = ""
    actor: str = ""
    agent: str = ""
    fence: int = 0
    version: int = 0
    reason: str = ""
    patch: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    ts: datetime


class InboxPayload(BaseModel):
    org: str
    inbox: str
    owner_agent: str
    owner_namespace: str
    description: str = ""
    posters: list[str] = Field(default_factory=list)
    accept_disclosure: list[str] = Field(default_factory=list)
    default_lease_seconds: int
    max_attempts: int
    max_defers: int
    triage_escalate_after_seconds: int
    webhook_url: str = ""
    created_by: str = ""
    # The principal that registered the inbox; it and the
    # service accounts it minted land todo without a policy row. "" = no
    # recorded owner (then only owner-namespace writers are trusted).
    owner_subject: str = ""
    created_at: datetime
    updated_at: datetime
    version: int


class PolicyPayload(BaseModel):
    org: str
    inbox: str
    poster: str
    kind: str
    mode: PolicyMode
    require_evidence: list[str] = Field(default_factory=list)
    set_by: str = ""
    set_at: datetime
    version: int


# ============================================================
# inbox.*
# ============================================================


class InboxRegisterInput(_Caller):
    """``namespace`` is the owner namespace (the caller must be its admin)."""

    # The inbox name, unique in the org; registering an existing name edits its settings.
    inbox: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    # The agent that works the inbox: a pull or claim naming no `agent` claims as it.
    owner_agent: str = Field(min_length=1, max_length=128)
    # Free text shown with the inbox; Loom does not interpret it.
    description: str = Field(default="", max_length=2000)
    # Subjects allowed to post besides writers on the owner namespace; an agent label
    # here admits only a caller without a token.
    posters: list[str] = Field(default_factory=list, max_length=200)
    # The `disclosure` levels a task may carry to land here; a create or reassign
    # with any other level is refused (422).
    accept_disclosure: list[Disclosure] = Field(default_factory=_all_disclosures)
    # Seconds a claim or heartbeat holds the lease when the call names no
    # `lease_seconds`.
    default_lease_seconds: int = Field(default=1800, ge=30, le=86_400)
    # Expired leases a task may reach; after that the sweeper blocks it on a human
    # instead of returning it to `todo`.
    max_attempts: int = Field(default=3, ge=1, le=20)
    # Defers a task may take; the next `task.defer` blocks it on a human instead.
    max_defers: int = Field(default=5, ge=1, le=50)
    # Seconds a task may wait in `triage` before the sweeper records one
    # `triage_escalated` event on it.
    triage_escalate_after_seconds: int = Field(default=14_400, ge=60, le=30 * 86_400)
    # Where signed wake-up notices (event kinds and task ids, never task text) are
    # POSTed: an http(s) URL on a public host (checked again at each delivery); ""
    # sends none.
    webhook_url: str = Field(default="", max_length=2048, pattern=r"^(https?://\S+)?$")
    # Name of the environment variable holding the webhook signing secret (never
    # stored): `LOOM_WEBHOOK_SECRET_<ORG>__<NAME>`, ORG the org id with a-z
    # upper-cased, digits kept and any other byte as `_` and two hex digits
    # (`acme-corp` → `ACME_2DCORP`), NAME letters and digits. Without it no notice
    # is sent.
    webhook_secret_ref: str = Field(
        default="", max_length=256, pattern=rf"^({WEBHOOK_SECRET_PREFIX}[A-Za-z0-9_]+)?$"
    )
    # The inbox `version` this edit was based on; a different current version
    # answers 409. None skips the check.
    if_match_version: int | None = None

    @model_validator(mode="after")
    def _own_secret(self) -> InboxRegisterInput:
        if self.webhook_secret_ref and not webhook_secret_ref_ok(self.org, self.webhook_secret_ref):
            raise ValueError(
                "webhook_secret_ref must be "
                f"{webhook_secret_org_prefix(self.org)}<letters and digits>"
            )
        return self


class InboxRegisterOutput(BaseModel):
    # The inbox as written.
    inbox: InboxPayload
    # True when this call registered a new inbox; false for a settings edit.
    created: bool


class InboxListInput(_Caller):
    """Inboxes the caller may see: those owned by ``namespace`` (the
    authorized namespace) plus, with ``mine_everywhere``, every inbox in the
    org the caller may post to: one whose owner namespace the caller can write,
    or that lists the caller as a poster (its subject, or its agent label without a
    token)."""

    # Also list every org inbox the caller may post to (write on its owner namespace,
    # or listed as a poster); false lists only inboxes owned by `namespace`.
    mine_everywhere: bool = False
    # At most this many inbox rows are read, 1-500 (with `mine_everywhere`, before
    # the visibility filter).
    limit: int = Field(default=200, ge=1, le=500)


class InboxListOutput(BaseModel):
    # The visible inboxes, ordered by name.
    inboxes: list[InboxPayload]


class InboxPrincipalsInput(_Caller):
    """The identities loom already knows in the org — the poster picker's
    source. ``inbox`` marks who can already post there and who is listed."""

    # The inbox whose posters are marked `listed`; an unregistered or unreadable name
    # answers against `namespace` with nobody listed.
    inbox: str
    # Case-insensitive substring matched against subject and label; "" returns everyone.
    query: str = Field(default="", max_length=128)
    # At most this many principals, 1-500; `truncated` says more matched.
    limit: int = Field(default=200, ge=1, le=500)


class Principal(BaseModel):
    subject: str
    label: str
    kind: Literal["human", "service_account", "other"]
    namespaces: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    last_seen: datetime | None = None
    # Already a writer on the inbox's owner namespace: may post without being listed.
    can_already_post: bool = False
    # Present in the inbox's posters list right now.
    listed: bool = False


class InboxPrincipalsOutput(BaseModel):
    # People first, then service accounts, then others, each sorted by label.
    principals: list[Principal]
    # True when more principals matched than `limit`.
    truncated: bool


class InboxPolicySetInput(_Caller):
    # The inbox the rule belongs to; the caller must be an admin of its owner namespace.
    inbox: str
    # Whom the rule covers: a subject, an agent label, or `*` for every poster. A label's
    # `auto` holds only for writers on the owner namespace.
    poster: str = Field(min_length=1, max_length=256)
    # The task kind the rule covers; `*` covers every kind. The most specific
    # (poster, kind) row wins.
    kind: str = Field(default="*", min_length=1, max_length=128)
    # `deny` refuses the post (403), `review` lands it in `triage`, `auto` lands it `todo`.
    mode: PolicyMode
    # Brief fields that must be present (a shape check at create), e.g.
    # ["inputs.finding_id"]. Loom never decides acceptance: a review task
    # waits until the inbox's agent or a person accepts it.
    require_evidence: list[str] = Field(default_factory=list, max_length=32)
    # True deletes the (poster, kind) rule instead of setting it.
    remove: bool = False


class InboxPolicySetOutput(BaseModel):
    # The rule as stored; null when `remove` deleted it.
    policy: PolicyPayload | None
    # True when the rule was deleted.
    removed: bool = False


class InboxPolicyListInput(_Caller):
    # The inbox whose rules to list; one the caller cannot read answers 404.
    inbox: str


class InboxPolicyListOutput(BaseModel):
    # Every live rule of the inbox, ordered by poster then kind.
    policies: list[PolicyPayload]


# ============================================================
# task.create / accept
# ============================================================


class TaskCreateInput(_Caller):
    """``namespace`` is the poster's own namespace; ``inbox`` the target."""

    # The target inbox; one the caller may neither post to nor read answers 404.
    inbox: str
    # The task type, chosen by the poster; policies and pull filters match on it.
    kind: str = Field(min_length=1, max_length=128)
    # One-line summary shown in lists.
    title: str = Field(min_length=1, max_length=500)
    # The task body as a JSON object; every `require_evidence` path of the matching
    # policy must be non-empty here (else 422).
    brief: dict[str, Any] = Field(default_factory=dict)
    # 1 (most urgent) to 4; pulls take lower numbers first, then the oldest.
    priority: int = Field(default=3, ge=1, le=4)
    # How sensitive the brief is: `confidential`, `internal` or `public_ok`; the inbox
    # must list it in `accept_disclosure`.
    disclosure: Disclosure = "internal"
    # Pulls and claims skip the task until this time (naive = UTC); None makes it
    # available at once.
    not_before: datetime | None = None
    # When the poster needs it done (naive = UTC); stored and shown, nothing enforces it.
    deadline: datetime | None = None
    # Free text naming where the task came from; stored and returned, never interpreted.
    origin: str = Field(default="", max_length=512)
    # The task this one follows from (a task id); None for a top-level task.
    parent_task_id: str | None = None
    # A retry by the same poster with the same key into the same inbox returns the
    # first task with `created=false`; another poster's same key stores its own task;
    # "" never deduplicates.
    idempotency_key: str = Field(default="", max_length=256)
    # Dev mode only (no token): who is posting.
    from_subject: str = Field(default="", max_length=256)


class TaskCreateOutput(BaseModel):
    # The task as stored, or the earlier one on an idempotent replay.
    task: TaskPayload
    # False when `idempotency_key` matched an earlier task.
    created: bool
    # The returned row's version; on a new task, the seq of its `created` event.
    seq: int
    # What happens next when the task lands in `triage` — the
    # inbox's owner accepts it; the poster cannot, and heartbeat / complete /
    # defer refuse a triage task. "" when it landed `todo`, so an agent reads what
    # to do instead of learning it from refused calls.
    next: str = ""


class TaskAcceptInput(_Caller):
    # The `triage` task to accept; its poster may not accept it (403).
    task_id: str
    # Optional edits the approver makes while accepting.
    priority: int | None = Field(default=None, ge=1, le=4)
    # Text stored in the brief as `constraints`, replacing any there; None leaves
    # the brief alone.
    constraints: str | None = None
    # Free text stored on the `accepted` event.
    note: str = Field(default="", max_length=4000)


class TaskOutput(BaseModel):
    # The task after the transition.
    task: TaskPayload
    # The seq of the event this call wrote.
    seq: int = 0


# ============================================================
# task.pull / claim / owner writes
# ============================================================


class TaskPullInput(_Caller):
    """``namespace`` is the inbox's owner namespace; the caller must be a
    writer there. ``status`` = ``triage`` returns review items to the owner
    without claiming them (the bot's own triage pass)."""

    # The inbox to pull from.
    inbox: str
    # Only tasks of these kinds; empty takes every kind.
    kinds: list[str] = Field(default_factory=list, max_length=64)
    # At most this many tasks per call, 1-20.
    limit: int = Field(default=1, ge=1, le=20)
    # Seconds to long-poll while nothing is available, up to 30; 0 returns at once.
    wait_seconds: int = Field(default=0, ge=0, le=MAX_WAIT_SECONDS)
    # Lease length of each claim; None uses the inbox's `default_lease_seconds`.
    lease_seconds: int | None = Field(default=None, ge=30, le=86_400)
    # `todo` claims tasks under a lease; `triage` lists review items without claiming.
    status: Literal["todo", "triage"] = "todo"


class TaskPullOutput(BaseModel):
    # The tasks claimed (or listed, for `triage`), by priority then age; empty when
    # nothing arrived in time.
    tasks: list[TaskPayload]
    # Milliseconds the call spent, long-poll wait included.
    waited_ms: int
    # The lease arbiter that took the claims: `keeper` or `log`.
    backend: str
    # An empty todo pull says how many wait in triage.
    note: str = ""


class TaskClaimInput(_Caller):
    # The `todo` task to claim; one whose `not_before` is still ahead answers 409.
    task_id: str
    # Lease length; None uses the inbox's `default_lease_seconds`.
    lease_seconds: int | None = Field(default=None, ge=30, le=86_400)


class TaskFenceInput(_Caller):
    """An owner write: ``fence`` is the token the claim returned."""

    # The `in_progress` task this owner write addresses.
    task_id: str
    # The fence token the claim returned; a stale one (the lease moved on) answers 409.
    fence: int = Field(ge=0)


class TaskHeartbeatInput(TaskFenceInput):
    # Extend the lease this many seconds from now; None uses the inbox default.
    lease_seconds: int | None = Field(default=None, ge=30, le=86_400)


class TaskProgressInput(TaskFenceInput):
    # Progress text recorded on a `progress` event; the lease is not extended.
    note: str = Field(min_length=1, max_length=8000)


class TaskDeferInput(TaskFenceInput):
    # Why the task is parked; stored on the row and the event.
    reason: str = Field(min_length=1, max_length=2000)
    # When the sweeper returns it to `todo`; None parks it until `task.resume`.
    resume_at: datetime | None = None


class TaskBlockInput(TaskFenceInput):
    # Why the task cannot proceed; stored on the row and the event.
    reason: str = Field(min_length=1, max_length=2000)
    # Who must act: `sender` (the poster may then unblock it), `human` or `external`.
    blocked_on: BlockedOn = "human"


class TaskCompleteInput(TaskFenceInput):
    # The result as a JSON object, stored on the task.
    outcome: dict[str, Any] = Field(default_factory=dict)
    # Free text stored on the `done` event.
    note: str = Field(default="", max_length=8000)


class TaskFailInput(TaskFenceInput):
    # Why it failed; stored on the row and the event.
    reason: str = Field(min_length=1, max_length=2000)
    # Any partial result as a JSON object, stored on the task.
    outcome: dict[str, Any] = Field(default_factory=dict)


# ============================================================
# task.wont_do / cancel / reopen / unblock / update / reassign
# ============================================================


class TaskReasonInput(_Caller):
    """A transition anyone entitled may make, with a reason; ``fence`` is
    required only when the caller is the current owner of an in_progress task."""

    # The task to move: an unfinished one, or a failed, wont_do or cancelled one for
    # `task.reopen`.
    task_id: str
    # Why; stored on the row and the event.
    reason: str = Field(min_length=1, max_length=2000)
    # The claim's fence, checked against an `in_progress` task when given; None skips
    # the check.
    fence: int | None = None


class TaskResumeInput(_Caller):
    """A deferred task back to ``todo`` now, ahead of its ``resume_at``."""

    # The `deferred` task to return to `todo`.
    task_id: str
    # Free text stored on the `resumed` event.
    note: str = Field(default="", max_length=4000)


class TaskUnblockInput(_Caller):
    # The `blocked` task to return to `todo`.
    task_id: str
    # Free text stored on the `unblocked` event.
    note: str = Field(default="", max_length=4000)


class TaskUpdateInput(_Caller):
    """Edit the brief. ``if_match_version`` must equal the current row's
    version or the write is refused with the current row (409)."""

    # The task to edit; allowed while it is `triage`, `todo`, `blocked` or `deferred`.
    task_id: str
    # The task `version` the edit is based on; a mismatch answers 409 with the
    # current row.
    if_match_version: int
    # New title; None keeps it.
    title: str | None = Field(default=None, min_length=1, max_length=500)
    # Replaces the whole brief; None keeps it.
    brief: dict[str, Any] | None = None
    # New priority, 1-4; None keeps it.
    priority: int | None = Field(default=None, ge=1, le=4)
    # New deadline; None keeps it (see `clear_deadline`).
    deadline: datetime | None = None
    # New earliest start; None keeps it (see `clear_not_before`).
    not_before: datetime | None = None
    # True removes the deadline; wins over `deadline`.
    clear_deadline: bool = False
    # True removes `not_before`; wins over `not_before`.
    clear_not_before: bool = False
    # Free text stored on the `updated` event; a note alone is a valid update.
    note: str = Field(default="", max_length=4000)


class TaskUpdateOutput(BaseModel):
    # The task after the edit.
    task: TaskPayload
    # The seq of the `updated` event.
    seq: int
    # Always false: a version mismatch answers 409 with the current row instead.
    conflict: bool = False


class TaskReassignInput(_Caller):
    # The unfinished task to move; it is cancelled in its current inbox.
    task_id: str
    # The destination; the caller must be able to post to it (as for task.create), it
    # must accept the task's disclosure, and its policy for the original poster
    # decides `todo` or `triage`.
    to_inbox: str
    # Why; stored on both the cancelled task and the replacement.
    reason: str = Field(min_length=1, max_length=2000)


class TaskReassignOutput(BaseModel):
    # The original task, now `cancelled`.
    task: TaskPayload
    # The new task in `to_inbox`; its `parent_task_id` is the original.
    replacement: TaskPayload


# ============================================================
# reads
# ============================================================


class TaskGetInput(_Caller):
    # The task to read.
    task_id: str


class TaskGetOutput(BaseModel):
    # False when the task does not exist or the caller may not read it.
    found: bool
    # The task; null when not found.
    task: TaskPayload | None = None


class TaskListInput(_Caller):
    """Tasks in ``inbox`` (owner side) or, with ``posted_by_me``, the tasks
    the caller posted anywhere in the org."""

    # The inbox to list (owner side); required unless `posted_by_me`.
    inbox: str | None = None
    # True lists the caller's own posts across the org; `inbox`, `posted_by` and
    # `owner` are then ignored.
    posted_by_me: bool = False
    # Owner side: the tasks one poster (subject or agent) sent to ``inbox`` —
    # the track record shown beside a triage decision.
    posted_by: str | None = Field(default=None, max_length=256)
    # Only these statuses; empty lists every status.
    statuses: list[TaskStatus] = Field(default_factory=list)
    # Only these kinds; empty lists every kind.
    kinds: list[str] = Field(default_factory=list, max_length=64)
    # Only tasks currently claimed by this agent.
    owner: str | None = None
    # At most this many rows, 1-500; `truncated` says more matched.
    limit: int = Field(default=100, ge=1, le=500)
    # Columns to return per row (identity columns always ride along, unknown names
    # are dropped); empty returns full task payloads.
    fields: list[str] = Field(default_factory=list, max_length=40)
    # Newest first instead of the work-queue order.
    newest_first: bool = False


class TaskListOutput(BaseModel):
    # One object per task, by priority then age unless `newest_first`.
    tasks: list[dict[str, Any]]
    # True when more tasks matched than `limit`.
    truncated: bool


class TaskCountsInput(_Caller):
    # Count one inbox; None counts every inbox owned by `namespace`.
    inbox: str | None = None


class InboxCount(BaseModel):
    inbox: str
    status: str
    # Set on blocked rows: sender | human | external — the bell counts the
    # human ones.
    blocked_on: str = ""
    n: int
    oldest: datetime | None = None


class TaskCountsOutput(BaseModel):
    # One row per (inbox, status, blocked_on) with its count and oldest `created_at`.
    counts: list[InboxCount]


class TaskHistoryInput(_Caller):
    # The task whose events to read.
    task_id: str
    # At most this many events, 1-1000, oldest first.
    limit: int = Field(default=200, ge=1, le=1000)


class TaskHistoryOutput(BaseModel):
    # False when the task does not exist or the caller may not read it.
    found: bool
    # The task's events in seq order.
    events: list[TaskEventPayload]


class TaskUpdatesInput(_Caller):
    """The sender's cursor: events on tasks this caller posted (or, with
    ``inbox``, every event in an inbox the caller owns) after ``since_seq``."""

    # Return events after this seq: pass the previous `next_seq`; 0 starts at the
    # beginning.
    since_seq: int = Field(default=0, ge=0)
    # Read every event of this inbox (one the caller can read) instead of the events
    # on the caller's own posts.
    inbox: str | None = None
    # At most this many events per call, 1-1000.
    limit: int = Field(default=200, ge=1, le=1000)
    # Seconds to long-poll while no event is available, up to 30; 0 returns at once.
    wait_seconds: int = Field(default=0, ge=0, le=MAX_WAIT_SECONDS)
    # Dev mode only (no token): whose posts to follow; a token's subject wins.
    from_subject: str = Field(default="", max_length=256)


class TaskUpdatesOutput(BaseModel):
    # Events in seq order; the last few seconds are held back so a row still landing
    # on another replica is not skipped.
    events: list[TaskEventPayload]
    # The cursor for the next call: the last event's seq, or `since_seq` when none.
    next_seq: int
    # Milliseconds the call spent, long-poll wait included.
    waited_ms: int
