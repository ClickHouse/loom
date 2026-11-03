"""Namespace wire models + type aliases; ops_namespace.py declares the operations."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.memory.domain.classify_endpoints import ClassifyEndpoint
from server.memory.domain.code_change import CodeChangePolicy
from server.memory.domain.proposal_policy import ProposalPolicy
from server.memory.domain.rerank_names import RerankName
from server.operations.base import GrantRoleStr

VisibilityStr = Literal["private", "public"]
# What lives in the namespace. 'memory' is the agent-memory default; 'code'
# marks one indexed repo@branch of the code-intelligence plane (read-only
# reference data, surfaced in the dashboard's Repositories page, hidden from memory views);
# 'bench' is a throwaway harness namespace: memory-shaped in
# every view, and the namespace_gc worker erases it once idle.
NamespaceKindStr = Literal["memory", "code", "bench"]
TrafficClassStr = Literal["production", "interactive", "bench"]


# newest_wins: a write whose extracted triple names the same
# subject and single-valued relation as a live memory with a different, older
# object retires that memory (status superseded, invalid_at stamped, history
# kept). Under newest_wins and auto_reinforce, an update the conflict resolver is
# sure of retires the older row too; under manual_review it is only flagged.
# append_only: the namespace's writes are events, not claims
# — a run's report, a finding dated by its run. Every write is its own row:
# the gate answers `clear`, no merge / duplicate / conflict verdict against a
# peer, a same-key rewrite stays last-writer-wins. The writer owns dedup.
ConflictPolicyStr = Literal["auto_reinforce", "manual_review", "newest_wins", "append_only"]


class NamespaceTypeConfig(BaseModel):
    """Per-(org, namespace, memory_type) configuration.

    Unset fields fall back to namespace-level defaults.
    """

    memory_type: str = Field(..., min_length=1, max_length=64)
    retention_days: int = Field(default=0, ge=0, le=36500)
    dedup_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    hot_quota: int = Field(default=0, ge=0, le=1000)
    conflict_policy: ConflictPolicyStr | None = None


# The search modes memory.search accepts (MemorySearchInput.search_mode).
_SearchModeStr = Literal[
    "semantic", "lexical", "hybrid", "bm25_hybrid", "related", "graph", "rrf", "temporal"
]
_BodiesStr = Literal["full", "excerpt", "none", "unit", "source"]


class NamespaceRetrievalDefaults(BaseModel):
    """What an unpinned memory.search on this namespace runs.

    Every knob is optional; a set one is applied to a search whose caller did
    not send that field — after a recipe (a caller choice), before the
    deployment default. A caller's explicit value always wins, and the
    planner's shape rules still run on a defaulted mode.
    `{}` clears the namespace's defaults; omitting the field on a
    re-register keeps them. Unknown keys are rejected so a misspelt knob is
    an error, not a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    search_mode: _SearchModeStr | None = None
    top_k: int | None = Field(default=None, ge=1, le=500)
    auto_touch: bool | None = None
    include_tags: bool | None = None
    bodies: _BodiesStr | None = None
    reinforcement_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    # An investigation store's "latest run" question wants the
    # newest dated candidate first; the namespace declares it once here.
    recency_pool_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    # A store whose similarity band sits above the embedder's
    # calibrated floor declares where its own noise starts, and what happens
    # to a hit below it (readmit / ids_only / drop).
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    floor_mode: Literal["readmit", "ids_only", "drop"] | None = None
    # A store of reports wants rows before passages in the window: the
    # share of the window verbatim chunks may take (per
    # namespace).
    chunk_max_share: float | None = Field(default=None, ge=0.0, le=1.0)


class NamespaceRegisterInput(BaseModel):
    org: str
    namespace: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Recommend kebab-case, e.g. eng-tooling",
    )
    # not EmailStr — local-only dev may use non-email strings
    owner_email: str = Field(
        description=(
            "The namespace's owner; an account with a login (an email, or an agent's claim "
            "code) keeps its stored owner exactly, so send that value back to update settings."
        )
    )
    # Every setting left None (or omitted) keeps the stored value, and a fresh row takes the
    # default named here, so a re-register that sends only what it changes resets nothing.
    # `tags` are the exception: the list is replaced whole, since it carries the
    # `auto-registered` marker the update gate reads.
    description: str | None = None  # fresh: ''
    # None = keep the existing row's kind (a fresh row is classified by name:
    # `bench` under a bench prefix, else `memory`), so a settings-only
    # re-register never demotes a code namespace to memory.
    kind: NamespaceKindStr | None = None
    # Labels on the namespace; the list is replaced whole, so a register that omits it
    # clears the stored tags.
    tags: list[str] = Field(default_factory=list)
    # The class requests into this namespace run as when the
    # token carries no `traffic_class` claim. Stored on the row as the tag
    # `traffic_class:<value>` (the onboarding kit's spelling); a bench class
    # with no explicit `kind` registers the namespace as `kind = bench`.
    traffic_class: TrafficClassStr | None = None
    active: bool | None = None  # fresh: True; True reactivates a soft-deleted row
    # None = preserve the current value (a privacy toggle must not be silently
    # re-enabled by a settings-form re-register that omits it), and False for a
    # namespace that doesn't exist yet — transcript capture is opt-in. Set
    # explicitly here, or via conversation.set_logging.
    conversation_logging_enabled: bool | None = None
    # Consent for sending this namespace's prompt/completion text to Langfuse.
    # None = inherit the server default; False = never, whatever the server says.
    # Omitted keeps the stored choice; an explicit null returns it to inherit.
    # Token counts, cost, quality scores and prompt-version links are unaffected —
    # they carry no user content, and gating them would make a privacy choice also
    # a cost-blindness choice.
    capture_generation_io: bool | None = None
    # The extraction coverage sweep per namespace.
    # None = the density gates decide per ingest; True/False override them.
    # Omitted keeps the stored choice; an explicit null returns it to the gates.
    coverage_sweep: bool | None = None
    consolidation_enabled: bool | None = None  # background memory.consolidate
    # 0.0 inherits the global cap.
    daily_cost_cap_usd: float | None = Field(default=None, ge=0.0)
    # Omitted keeps the stored threshold; an explicit null inherits LOOM_CONFLICT_THRESHOLD.
    conflict_threshold: float | None = None
    # 0 = inherit global retention/tiering; positive overrides per-(org, namespace).
    retention_days: int | None = Field(default=None, ge=0, le=36500)
    # Rows older than this many days move to the warm storage tier; 0 inherits the global
    # tiering setting.
    warm_days: int | None = Field(default=None, ge=0, le=36500)
    # `private` = members-only; `public` = read-by-anyone-in-org + appears in
    # namespace.browse. Write access still requires membership.
    visibility: VisibilityStr | None = None  # fresh: private
    # When true, writer/admin self-joins go pending; viewer stays auto-approved.
    requires_approval: bool | None = None
    # How the namespace is joined. None keeps the stored value
    # (a settings write never resets it); `inherit` derives the rule from
    # visibility + requires_approval.
    join_policy: Literal["inherit", "open", "approval", "closed"] | None = None
    # The retrieval knobs an unpinned memory.search on this
    # namespace runs with (mode, k, auto_touch, include_tags, bodies,
    # reinforcement_weight). None keeps the stored value; `{}` clears it.
    # A namespace admin sets it (the same gate as every other field here).
    retrieval_defaults: NamespaceRetrievalDefaults | None = None
    # Optional reranker for memory.search: "builtin:openai[:<model>]",
    # "builtin:cohere[:<model>]" or the name of an endpoint in
    # LOOM_RERANK_ENDPOINTS_JSON (a URL is refused); empty ⇒ cosine + entity_boost.
    # builtin:cohere is refused while the gateway is required.
    # The prompt template is passed through opaquely, not interpreted.
    rerank_endpoint: RerankName | None = None
    # Text forwarded as `prompt_template` in every call to `rerank_endpoint`.
    rerank_prompt_template: str | None = None
    # Optional pluggable classifier for memory.set(classify=True). Out-of-registry
    # responses are ignored; fail-open on any error. While the gateway is required
    # its host must be in LOOM_INFERENCE_GATEWAY_HOSTS (else 422).
    classify_endpoint: ClassifyEndpoint = None
    # Text forwarded as `prompt_template` in every call to `classify_endpoint`.
    classify_prompt_template: str | None = None
    # Per-tag TTL + dedup_threshold registry, matched on tags at memory.set time.
    # Empty ⇒ namespace-level defaults apply uniformly.
    tag_registry: list[NamespaceTypeConfig] = Field(default_factory=list)
    default_conflict_policy: ConflictPolicyStr | None = None  # fresh: auto_reinforce
    # How proposals (facts written with memory.set
    # proposed=true) are decided. None keeps the stored policy; the default
    # ({"mode": "manual"}) clears it. Only an owner-set `auto` lets loom accept
    # or reject a proposal on its graded runs without a person.
    proposal_policy: ProposalPolicy | None = None
    # How reads treat this namespace's code-linked memories when their code changes:
    # `retire_removed` (the default: search leaves out a memory whose code is all gone,
    # and serves it again when the code returns), `flag` (only the links are marked) or
    # `revise` (as retire_removed, and a changed claim is re-checked against the new code;
    # one that no longer holds gets a suggested correction, from the code's next edit on).
    # Loom never writes the memory. None keeps the stored policy.
    code_change_policy: CodeChangePolicy | None = None


class NamespaceRegisterOutput(BaseModel):
    registered: bool  # False = no change; record already matched
    namespace: str
    # When the registry row was written (UTC, whole seconds).
    updated_at: datetime
    # The visibility stored by this write (`private` for a restricted namespace).
    visibility: VisibilityStr = "private"


class NamespaceListInput(BaseModel):
    org: str
    # True lists active namespaces only; false includes soft-deleted ones.
    active_only: bool = True
    tags: list[str] = Field(default_factory=list)  # ANY-match filter
    # At most this many rows, in namespace-name order; 1 to 2,000.
    limit: int = Field(default=200, ge=1, le=2000)


class _NamespaceEntry(BaseModel):
    namespace: str
    kind: str = "memory"
    owner_email: str
    description: str
    tags: list[str]
    active: bool
    consolidation_enabled: bool = False
    daily_cost_cap_usd: float = 0.0
    conflict_threshold: float | None = None
    retention_days: int = 0
    warm_days: int = 0
    visibility: VisibilityStr = "private"
    created_at: datetime
    updated_at: datetime


class NamespaceListOutput(BaseModel):
    # The org's registry rows matching the filters, in namespace-name order.
    rows: list[_NamespaceEntry]


class NamespaceGetInput(BaseModel):
    org: str
    namespace: str


class NamespaceGetOutput(BaseModel):
    # False when the namespace has no registry row; every other field is then None.
    found: bool
    namespace: str
    # `memory`, `code` (an indexed repository branch) or `bench` (a throwaway harness).
    kind: str | None = None
    # The subject named as owner when the namespace was registered.
    owner_email: str | None = None
    # Free-text description; empty when none was set.
    description: str | None = None
    # The namespace's labels, including markers such as `auto-registered`.
    tags: list[str] | None = None
    # False once the namespace is soft-deleted (its data stays in place).
    active: bool | None = None
    # True when conversation transcripts are captured; false when off or never set.
    conversation_logging_enabled: bool | None = None
    # Whether prompt/completion text goes to Langfuse; None inherits the server default.
    capture_generation_io: bool | None = None
    # Forces the extraction coverage sweep on or off; None lets the density gates decide.
    coverage_sweep: bool | None = None
    # True when background `memory.consolidate` runs on this namespace.
    consolidation_enabled: bool | None = None
    # Upstream spend cap per rolling 24 hours, in USD; 0 inherits the global cap.
    daily_cost_cap_usd: float | None = None
    # Cosine at which a write is flagged `conflicted`; None inherits the server threshold.
    conflict_threshold: float | None = None
    # Rows older than this many days are deleted; 0 inherits the global retention.
    retention_days: int | None = None
    # Rows older than this many days move to the warm tier; 0 inherits the global tiering.
    warm_days: int | None = None
    # `private` (members only) or `public` (readable org-wide, listed by `namespace.browse`).
    visibility: VisibilityStr | None = None
    # True when writer/admin self-joins wait for an admin's approval.
    requires_approval: bool | None = None
    # The stored word and the resolved one (inherit folded).
    join_policy: str | None = None
    # `open`, `approval` or `closed`: the join rule in force once `inherit` is resolved.
    effective_join_policy: str | None = None
    # The set retrieval knobs; None when the namespace has none.
    retrieval_defaults: dict[str, Any] | None = None
    # The reranker `memory.search` uses (a builtin or a configured endpoint's name);
    # empty when none.
    rerank_endpoint: str | None = None
    # URL `memory.set(classify=true)` calls to classify a write; empty when none.
    classify_endpoint: str | None = None
    # How close writes are settled: `auto_reinforce` (the default when unset),
    # `manual_review`, `newest_wins` or `append_only`.
    default_conflict_policy: str | None = None
    # How proposals (`memory.set proposed=true`) are decided; the manual default when unset.
    proposal_policy: ProposalPolicy | None = None
    # How reads treat the namespace's code-linked memories when their code changes:
    # `retire_removed` (the default), `flag` or `revise`.
    code_change_policy: CodeChangePolicy | None = None
    # Per-tag configuration rows (retention, dedup threshold, hot quota, conflict policy);
    # empty when none are registered.
    tag_registry: list[dict[str, Any]] | None = None
    # `standard` or `restricted` (loom.namespaces.classification). A restricted row is
    # updated only by a named-namespace ADMIN token with a direct grant; clients mirror
    # that to hide settings they would be refused. None when not found.
    classification: Literal["standard", "restricted"] | None = None
    # The current revision of the namespace's guide, the `expected_revision` of its next
    # `namespace.guide_set`; 0 when it never had one.
    guide_revision: int | None = None
    # The guide's one-line summary; empty when there is no guide or it was cleared.
    guide_summary: str | None = None
    # When the namespace was first registered (UTC).
    created_at: datetime | None = None
    # When the registry row was last written (UTC).
    updated_at: datetime | None = None


class NamespaceDeleteInput(BaseModel):
    org: str
    namespace: str


class NamespaceDeleteOutput(BaseModel):
    deleted: bool  # True if the row went from active=true → false
    namespace: str
    # When the soft-delete was written, or the time of the call when nothing changed.
    updated_at: datetime


class NamespaceBrowseInput(BaseModel):
    org: str
    # Substring match on the namespace name or description, or on a public
    # namespace's guide summary.
    query: str = ""
    # At most this many rows, most members first; `closed` rows are dropped after the limit.
    limit: int = Field(default=50, ge=1, le=500)
    # `closed` rows (bench/scratch) are omitted unless asked for;
    # `kinds` narrows the directory (e.g. ["code"] for the Code view).
    include_closed: bool = False
    # Only these kinds (`memory`, `code`, `bench`); empty lists every kind.
    kinds: list[str] = Field(default_factory=list)


class _BrowseEntry(BaseModel):
    namespace: str
    kind: str = "memory"
    description: str
    owner_email: str
    member_count: int
    requires_approval: bool = False
    created_at: datetime
    # The resolved join policy (open | approval | closed), the
    # caller's standing on the row (none | pending | member), and for a code
    # namespace the repository and branch it indexes.
    join_policy: str = "approval"
    visibility: str = "public"
    my_status: Literal["none", "pending", "member"] = "none"
    my_role: str = ""
    repo: str = ""
    branch: str = ""
    # The current revision of the namespace's guide (the `expected_revision` of its next
    # `namespace.guide_set`) and its one-line summary, for a public row or one the caller
    # belongs to; 0 and "" when it never had a guide, and "" when the guide was cleared.
    guide_revision: int = 0
    guide_summary: str = ""


class NamespaceBrowseOutput(BaseModel):
    # Joinable namespaces (public rows and code namespaces), most members first, each with
    # the caller's standing.
    rows: list[_BrowseEntry]


class NamespaceJoinInput(BaseModel):
    org: str
    namespace: str
    # Role to self-grant. viewer auto-activates; writer/admin ALWAYS land
    # pending (regardless of requires_approval) — an existing admin approves
    # via grants.approve. The creator is auto-granted admin at register time.
    role: GrantRoleStr = "writer"
    # Dev-mode override; ignored when a JWT is present.
    subject: str | None = None


class NamespaceJoinOutput(BaseModel):
    # True when this call granted access or recorded an elevation on an existing grant;
    # false for a no-op or a request that left the caller with no access yet.
    joined: bool
    namespace: str
    # The role that actually took effect now. A gated writer/admin request
    # auto-grants viewer so the user is functional while waiting.
    role: GrantRoleStr
    # `active` when the role took effect, `pending` while a request waits for an admin.
    status: str = "active"
    # The role queued for, when a writer/admin request was gated.
    requested_role: GrantRoleStr | None = None
    # Active admins who can approve the pending request; empty when nothing is pending.
    pending_admins: list[str] = Field(default_factory=list)
    # When the grant row was written; for a no-op, the existing grant's time.
    joined_at: datetime


class NamespaceLeaveInput(BaseModel):
    org: str
    namespace: str
    # Who leaves, used only when the request carries no authenticated caller (dev mode).
    subject: str | None = None


class NamespaceLeaveOutput(BaseModel):
    # True when an active grant was revoked; false when the caller held none.
    left: bool
    namespace: str
    # When the grant was revoked (UTC); None when nothing was revoked.
    left_at: datetime | None = None


class NamespaceCancelRequestInput(BaseModel):
    """Withdraw the caller's own pending elevation request.

    Keeps the current (lower) role and clears `pending_role`. Self-only;
    admin-driven cancellation goes through grants.approve(decision='reject').
    """

    org: str
    namespace: str
    # Whose request to withdraw, used only when the request carries no authenticated
    # caller (dev mode).
    subject: str | None = None


class NamespaceCancelRequestOutput(BaseModel):
    cancelled: bool  # False if there was no pending request
    namespace: str
    # The role the withdrawn request asked for; None when nothing was cancelled.
    cleared_role: GrantRoleStr | None = None
    # When the request was withdrawn (UTC); None when nothing was cancelled.
    cancelled_at: datetime | None = None


class NamespaceListMineInput(BaseModel):
    org: str
    # Whose memberships to list, used only when the request carries no authenticated
    # caller (dev mode).
    subject: str | None = None


class _MineEntry(BaseModel):
    namespace: str
    role: str  # 'viewer' | 'writer' | 'admin' | 'jwt'
    # 'pending' here = a no-base-access request (role IS the request); an
    # elevation request on an existing grant stays 'active' + sets pending_role.
    status: str = "active"
    # Role queued for on a gated namespace; None when no request is outstanding.
    pending_role: str | None = None
    pending_since: datetime | None = None
    # Admins who could approve; populated only when pending_role is set.
    pending_admins: list[str] = Field(default_factory=list)
    visibility: VisibilityStr = "private"
    kind: str = "memory"
    description: str = ""
    joined_at: datetime | None = None
    # True when unvisited since the grant; False for JWT-only namespaces.
    is_new: bool = False
    # True on a row a client token (`sa:` subject) reads
    # through its creator's live grant rather than a grant of its own — the
    # role shown is the creator's. Restricted namespaces are never inherited
    # and never appear this way.
    inherited: bool = False


class NamespaceListMineOutput(BaseModel):
    # The caller's grant rows (newest first), then namespaces named only by the token
    # (role `jwt`), then those a client token reads through its creator.
    rows: list[_MineEntry]


class NamespaceMarkSeenInput(BaseModel):
    org: str
    namespace: str
    # Dev-mode fallback; the JWT sub is authoritative in production.
    subject: str | None = None


class NamespaceMarkSeenOutput(BaseModel):
    namespace: str
    # When the visit was recorded (UTC).
    seen_at: datetime


class _SnapshotMemoryRow(BaseModel):
    """One memory row in a snapshot payload: every persisted loom.memory
    column but org, namespace (the restore target supplies them) and
    tombstoned_at, with `version` and `created_at` kept so a restore writes
    a full version row."""

    memory_id: str
    agent: str
    user_id: str
    session_id: str
    run_id: str
    memory_key: str
    memory_type: str
    value: str  # already-serialized JSON
    confidence: float
    source_trace: str
    pinned: bool
    expires_at: datetime | None = None
    entities: list[str] = Field(default_factory=list)
    # Kept so a restore is faithful. `version` and
    # `content_sha256` also make the payload a manifest of the namespace.
    version: int = 0
    content_sha256: str = ""
    actor: str = ""
    source_class: str = ""
    source_resource_id: str = ""
    verified_by: str = ""
    verified_at: datetime | None = None
    verified_sha256: str = ""
    grounding: str = ""
    evidence: str = ""
    tags: list[str] = Field(default_factory=list)
    valid_at: datetime | None = None
    # Stated validity end; a restore never strips it.
    valid_to: datetime | None = None
    # The remaining loom.memory columns, so a restore is a
    # full version row (`MemoryRow.from_snapshot` + `build_memory_version_row`)
    # rather than a partial row with `created_at` reset to now(). None /
    # empty means "not in this payload" and lands the model default on restore.
    created_at: datetime | None = None
    updated_at: datetime | None = None
    value_json: str = "{}"
    status: str = "active"
    conflict_with: list[str] = Field(default_factory=list)
    duplicate_of: str = ""
    evidence_count: int = 1
    recall_count: int = 0
    last_recalled_at: datetime | None = None
    cross_promoted_from_memory_id: str = ""
    cross_promoted_from_namespace: str = ""
    superseded_by: str = ""
    rejection_reason: str = ""
    temporal_anchor: datetime | None = None
    temporal_confidence: float = 0.0
    invalid_at: datetime | None = None
    char_limit: int | None = None
    reinforcement_count: int = 1
    last_reinforced_at: datetime | None = None
    mentioned_at: datetime | None = None
    sensitivity: str = ""
    redacted_at: datetime | None = None
    redaction_reason: str = ""
    lock_mode: str = ""
    lock_reason: str = ""
    lock_until: datetime | None = None
    locked_by: str = ""
    locked_at: datetime | None = None
    on_expire: str = ""
    expire_reason: str = ""
    files: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)

    @field_validator("value_json", mode="before")
    @classmethod
    def _value_json_as_text(cls, v: Any) -> Any:
        """`loom.memory.value_json` is a JSON column and the
        client returns it parsed (`{}` for the placeholder a non-object value
        leaves). The payload carries its text — what the JSON column accepts
        back on restore — so a parsed object (a raw-column read, an older
        payload) is dumped and text passes through unchanged."""
        if v is None:
            return "{}"
        if isinstance(v, (dict, list)):
            return json.dumps(v)
        if isinstance(v, bytes):
            return v.decode("utf-8", errors="replace")
        return v


class _SnapshotTypeRow(BaseModel):
    memory_type: str
    retention_days: int
    dedup_threshold: float | None = None
    hot_quota: int = 0
    conflict_policy: str = ""


class _SnapshotGrantRow(BaseModel):
    subject: str
    role: str
    granted_by: str


class _SnapshotAuditRow(BaseModel):
    ts: datetime
    event_type: str
    actor: str
    subject_json: str


class NamespaceSnapshotInput(BaseModel):
    """Freeze a namespace's curated state as a portable JSON payload.

    `include_embeddings=False` lets the target re-embed under its own model;
    set True only when source + target use the same embedder.
    """

    org: str
    namespace: str
    # Recorded on the snapshot row; the payload carries no vectors either way, so the
    # target re-embeds the restored rows.
    include_embeddings: bool = False
    # Recent audit rows to include for provenance; 0 skips.
    include_audit_tail_n: int = Field(default=1000, ge=0, le=100000)


class NamespaceSnapshotOutput(BaseModel):
    # A fresh UUID; `namespace.restore` uses it to skip a payload already restored.
    snapshot_id: str
    org: str
    # The namespace the payload was read from.
    source_namespace: str
    # When the snapshot was taken (UTC, whole seconds).
    created_at: datetime
    # Number of rows in `memories`.
    memory_count: int
    # Number of rows in `types`.
    type_count: int
    # Number of rows in `grants`.
    grant_count: int
    # Number of rows in `audit_tail`.
    audit_tail_count: int
    # Echoes the request; the payload carries no vectors either way.
    include_embeddings: bool
    # Every non-tombstoned memory row, oldest first, whatever its status.
    memories: list[_SnapshotMemoryRow]
    # The namespace's per-tag configuration rows.
    types: list[_SnapshotTypeRow]
    # Active grants only; pending requests and revoked grants are left out.
    grants: list[_SnapshotGrantRow]
    # The newest audit rows of the namespace, newest first, at most `include_audit_tail_n`.
    audit_tail: list[_SnapshotAuditRow]


class NamespaceRestoreInput(BaseModel):
    """Restore a snapshot payload into a target namespace.

    Idempotent: re-running with the same snapshot_id is a no-op (returns
    `restored=False`). Caller is expected to have ADMIN on the target.
    """

    org: str
    # Where the payload is written, never a collection (`col-…`, `@me`); created (with the
    # caller as admin) when absent, and reactivated when soft-deleted.
    target_namespace: str
    # A `namespace.snapshot` output, unchanged.
    payload: NamespaceSnapshotOutput
    # Rebind every memory row's `agent` to this when set; default preserves source.
    rebind_agent: str | None = None


class NamespaceRestoreOutput(BaseModel):
    # False when this `snapshot_id` was already restored into the target (nothing written).
    restored: bool
    # The namespace written to (echoes the request).
    target_namespace: str
    # The restored payload's id.
    snapshot_id: str
    # `memories`, `types` and `grants` as the payload counted them.
    counts: dict[str, int]


class NamespaceRollbackInput(BaseModel):
    """Return every memory head in a namespace to the version it had at
    `as_of`.

    Head-state rollback: for each key, the highest version stamped at or
    before `as_of` (from loom.memory_versions) is re-inserted as the new
    head; keys that did not exist yet are tombstoned. Embeddings are
    regenerated from the reinstated heads by the embedding worker. Defaults
    to a dry run that reports the plan; set `dry_run=false` to apply.
    Refuses an `as_of` older than the namespace's history or in the future.
    """

    org: str
    namespace: str
    # UTC instant to return to. Naive values are read as UTC.
    as_of: datetime
    # True reports the plan and writes nothing; false applies it.
    dry_run: bool = True
    # Restrict to these keys; None = every key in the namespace.
    memory_keys: list[str] | None = Field(default=None, max_length=500)
    # Upper bound on keys changed per call; the output says when it was hit.
    max_keys: int = Field(default=5000, ge=1, le=20000)


class NamespaceRollbackOutput(BaseModel):
    # Echoes the request: true when nothing was written.
    dry_run: bool
    # The instant rolled back to, as UTC.
    as_of: datetime
    # `as_of` as an epoch-nanosecond version: the highest version a key may keep.
    version_at: int
    # Keys whose head was (or would be) replaced by an older version.
    reinstate: int
    # Keys created after as_of that were (or would be) tombstoned.
    remove: int
    # Keys with history at as_of but no live row (hard-deleted): left alone.
    skipped_no_head: int = 0
    # Keys created after as_of that are already tombstoned.
    skipped_already_gone: int = 0
    # More keys differ than max_keys; call again (or narrow memory_keys).
    truncated: bool = False
    # Up to 20 planned moves (`memory_key`, `agent`, `action`, `version_from`, `version_to`).
    sample: list[dict[str, Any]] = Field(default_factory=list)
    # When the rollback was written; None on a dry run or when no key differed.
    applied_at: datetime | None = None


# ------------------------------------------------------------------
# namespace.validate — one read-only conformance report for a namespace.
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# namespace.audit — how a namespace is being USED, with the anti-patterns
# named. validate is the integrity of what was written;
# audit is the access pattern and value shape the ledger and the tables show.
# ------------------------------------------------------------------
