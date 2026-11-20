"""Request and response models of the `/v1/admin/*` routes: one input model
per endpoint, and the typed return shapes the OpenAPI document shows."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ============================================================
# Typed input models — one per endpoint
# ============================================================


class AuditSummaryInput(BaseModel):
    since_seconds: int = Field(86400, ge=1, le=86_400 * 90)
    org: str | None = None
    namespace: str | None = None


class EventsTailInput(BaseModel):
    since_seconds: int = Field(60, ge=1, le=86_400)
    limit: int = Field(100, ge=1, le=1000)
    org: str | None = None
    namespace: str | None = None
    agent: str | None = None
    operation: str | None = None
    success: bool | None = None


class BudgetViolationsInput(BaseModel):
    since_seconds: int = Field(600, ge=1, le=86_400)
    limit: int = Field(50, ge=1, le=1000)
    org: str | None = None


class BudgetSetInput(BaseModel):
    op: str
    budget_ms: int | None = Field(None, ge=0)
    clear: bool = False
    # With `consumer`, the override applies to that consumer
    # only (op "*" = every op for it); `budget_ms` may then be a "<n>x"
    # multiplier of the op's declared budget instead of milliseconds.
    consumer: str = Field(default="", max_length=64)
    budget: int | str | None = None


class ConsumersInput(BaseModel):
    """Per-consumer aggregates. One row per consumer per
    window: calls, errors, error rate, p50/p95, spend, outcome feedback."""

    windows_seconds: list[int] = Field(default_factory=lambda: [86_400, 7 * 86_400])
    limit: int = Field(200, ge=1, le=1000)
    org: str | None = None

    @field_validator("windows_seconds")
    @classmethod
    def _bounded_windows(cls, v: list[int]) -> list[int]:
        if not v or len(v) > 4:
            raise ValueError("1 to 4 windows")
        for w in v:
            if not 1 <= w <= 90 * 86_400:
                raise ValueError("each window must be 1 s .. 90 d")
        return v


class RatelimitSetInput(BaseModel):
    """Exactly one of `namespace` or `org` must be set. `clear` toggles
    install vs remove."""

    namespace: str | None = None
    org: str | None = None
    op_class: str
    # Finite: stored and adopted by every process.
    capacity: float | None = Field(None, gt=0, allow_inf_nan=False)
    rate: float | None = Field(None, ge=0, allow_inf_nan=False)
    clear: bool = False


class SettingsListInput(BaseModel):
    # Only the settings whose field name starts with this.
    prefix: str = Field(default="", max_length=128)
    # Only the settings off their default or overridden.
    changed: bool = False


class SettingSetInput(BaseModel):
    """Install a deployment-wide override of a live setting ({name, value}), or
    remove it ({name, clear: true})."""

    name: str = Field(max_length=128)
    # JSON, validated as the setting validates its environment value.
    value: Any = None
    clear: bool = False
    # Refuse (409 PRECONDITION_FAILED) unless this is the value in force after a fresh
    # read of the stored overrides, so a change made since through another replica or
    # the CLI is not overwritten (two in the same instant can still both pass).
    expected: Any = None

    @model_validator(mode="after")
    def _value_or_clear(self) -> SettingSetInput:
        if not self.clear and "value" not in self.model_fields_set:
            raise ValueError("value required when clear=false")
        return self


class FleetCostInput(BaseModel):
    since_seconds: int = Field(86400, ge=1)
    top_n: int = Field(20, ge=1, le=500)
    group_by: Literal["namespace", "agent", "org"] = "namespace"


class PromptsEvaluateInput(BaseModel):
    prompt_name: str
    prompt_version: int
    rubric: str = "correctness"
    min_evals: int = Field(5, ge=1)
    threshold: float = Field(0.85, ge=0.0, le=1.0)


class TokenMintInput(BaseModel):
    """The endpoint mints the same token the CLI does. `ns_roles` (namespace → viewer |
    writer | admin) is the scoped form — per-namespace roles, the read priority in map order —
    and `consumer` binds the credit tag to the credential. Both are needed to express a bot
    that is admin on its own memory namespace and viewer on many code / tracker namespaces (a
    flat `max(roles)` would refuse ["admin","viewer"] on a restricted namespace), and to have
    reads on the minted token credited to a consumer rather than answer
    `not_credited (no_consumer)`. `namespaces` may be omitted when `ns_roles` names them."""

    # Its name in the caller's token list; the token's subject is `sa:<name>-<hash>`.
    # Required: the handler answers a missing one, naming the retired `subject`.
    name: str | None = Field(None, max_length=64)
    org: str
    namespaces: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=lambda: ["writer"])
    ns_roles: dict[str, str] | None = None
    consumer: str = Field("", max_length=64)
    # None: 90 days; 0: never expires.
    ttl_seconds: int | None = Field(None, ge=0, le=3650 * 86400)
    # A token without `*`: where calls that name no namespace save, a memory namespace it
    # lists and may write (422 otherwise); None: the parent token's when it may write
    # there, else the first such one. Not beside `*`.
    saves_to: str | None = Field(None, max_length=256)

    @model_validator(mode="after")
    def _namespaces_or_ns_roles(self) -> TokenMintInput:
        if self.ns_roles:
            from server.auth.authz import GRANT_ROLES

            bad = {ns: r for ns, r in self.ns_roles.items() if r not in GRANT_ROLES}
            if bad:
                raise ValueError(f"ns_roles values must be viewer | writer | admin: {bad}")
            # the scoped map names the namespaces; `namespaces` may add read-only extras
            merged = list(dict.fromkeys([*self.namespaces, *self.ns_roles.keys()]))
            object.__setattr__(self, "namespaces", merged)
        if not self.namespaces:
            raise ValueError("namespaces (or ns_roles) must name at least one namespace")
        return self

    def requested_role_for(self, ns: str) -> str:
        """The role this mint asks for on `ns`: its `ns_roles` entry (viewer for
        a namespace the map leaves out), else the highest of the flat `roles`."""
        if self.ns_roles:
            return self.ns_roles.get(ns, "viewer")
        from server.auth.authz import grant_rank

        known = [r for r in self.roles if grant_rank(r)]
        return max(known, key=grant_rank) if known else "viewer"


class NamespaceAdoptInput(BaseModel):
    org: str
    namespace: str
    new_admin: str  # subject to install as admin


# ============================================================
# Output models — typed return shapes for OpenAPI
# ============================================================


class SchemaStatusOutput(BaseModel):
    expected: list[str]
    present: list[str]
    missing: list[str]
    extra: list[str]
    drift: bool


class WorkersStatusOutput(BaseModel):
    workers: dict[str, dict[str, Any]]
    # What the store (`loom.worker_status`) contributed — the
    # rows read and every launcher label that reported, so the web tier says
    # which colour's workers it is showing.
    store: dict[str, Any] = Field(default_factory=dict)


class AuditSummaryRow(BaseModel):
    event_type: str
    count: int
    first_ts: str
    last_ts: str


class AuditSummaryOutput(BaseModel):
    rows: list[AuditSummaryRow]
    total: int
    since_seconds: int


class EventTailRow(BaseModel):
    ts: str
    org: str
    namespace: str
    agent: str
    operation: str
    latency_ms: int
    success: bool
    error_code: str
    request_id: str
    trace_id: str


class EventsTailOutput(BaseModel):
    rows: list[EventTailRow]
    since_seconds: int


class BudgetViolationRow(BaseModel):
    namespace: str
    operation: str
    violations: int
    max_overage_ratio: float
    avg_overage_ratio: float
    last_violation_at: str | None


class BudgetViolationsOutput(BaseModel):
    rows: list[BudgetViolationRow]
    since_seconds: int
    total_violations: int


class BudgetSetOutput(BaseModel):
    ok: bool
    op: str
    budget_ms: int | None = None
    cleared: bool | None = None
    consumer: str | None = None
    budget: int | str | None = None


class ConsumerRow(BaseModel):
    consumer: str
    calls: int
    errors: int
    server_errors: int
    error_rate: float
    p50_ms: float
    p95_ms: float
    spend_usd: float
    outcome_calls: int
    outcome_rows: int = 0
    outcome_positive: int = 0
    outcome_negative: int = 0
    shunt_rows: int = 0
    tokens_saved: int = 0
    namespaces: int
    ops_distinct: int
    last_seen: str | None
    # How many of the calls (and how much of the spend) carried
    # a tag a token claim bound, and the row's label — `claimed` when every
    # call did, `self-reported` when none did, `mixed` otherwise. A subject
    # class (service-account / user / anonymous) is `class`.
    claimed_calls: int = 0
    claimed_spend_usd: float = 0.0
    attribution: str = "self-reported"


class ConsumerTaggingRow(BaseModel):
    """One token identity's request rows, split into the calls
    that carried an explicit `consumer` tag and the calls that fell back to
    the subject class — a client that tags part of its traffic is one row."""

    org: str
    agent: str
    calls: int
    tagged: int
    untagged: int
    tagged_ratio: float
    tag: str = ""
    tagged_ops: int = 0
    untagged_ops: int = 0


class ConsumersWindow(BaseModel):
    since_seconds: int
    rows: list[ConsumerRow]
    # Additive — an older reader ignores it.
    tagging: list[ConsumerTaggingRow] = Field(default_factory=list)
    # Of the spend under an explicit tag (not a subject class), the share that a token claim
    # bound.
    tagged_spend_usd: float = 0.0
    claim_bound_spend_usd: float = 0.0
    claim_bound_share: float = 0.0


class ConsumersOutput(BaseModel):
    windows: list[ConsumersWindow]
    budget_overrides_by_consumer: dict[str, dict[str, int | float]]


class RatelimitSetOutput(BaseModel):
    ok: bool
    scope: str
    key: str
    op_class: str
    capacity: float | None = None
    rate: float | None = None
    cleared: bool | None = None


class SettingRow(BaseModel):
    name: str
    env: str
    # A secret shows only its length.
    value: Any
    default: Any
    # override (settings.set), dashboard (Models & keys), env, env_file (the dotenv
    # file), settings_file (the YAML settings file), inferred (filled in at load from
    # other settings) or default.
    source: Literal[
        "override", "dashboard", "env", "env_file", "settings_file", "inferred", "default"
    ]
    # True: takes an override while Loom runs; False: read once at startup.
    live: bool
    description: str


class SettingsListOutput(BaseModel):
    settings: list[SettingRow]


class SettingsHistoryInput(BaseModel):
    # One setting's changes; "" = every setting's.
    name: str = Field(default="", max_length=128)
    limit: int = Field(default=50, ge=1, le=1000)


class SettingChange(BaseModel):
    ts: datetime
    # Who made it: a token subject, or `cli:<user>`.
    actor: str
    name: str
    value: Any
    previous: Any
    # `override`: undo by setting `previous` back. Anything else (env, default, …) was
    # the value on the process that made the change: undo with a reset. None for a
    # setting this build lacks (the reset of a retired setting's override).
    previous_source: str | None
    cleared: bool


class SettingsHistoryOutput(BaseModel):
    # Newest first.
    changes: list[SettingChange]


class SettingSetOutput(BaseModel):
    ok: bool
    # None after clearing the override of a setting this build does not have.
    setting: SettingRow | None


class FleetCostRow(BaseModel):
    key: str
    total_cost_usd: float
    total_calls: int
    ops_distinct: int


class FleetCostOutput(BaseModel):
    rows: list[FleetCostRow]
    since_seconds: int
    group_by: str
    top_n: int


class PromptsEvaluateOutput(BaseModel):
    should_promote: bool
    eval_count: int
    mean_score: float | None
    min_score: float | None
    threshold: float
    rationale: str
    prompt_name: str
    prompt_version: int


class TokenMintOutput(BaseModel):
    token: str
    # The token's `jti`: its id in the ledgers and the
    # handle `loom revoke-token --jti` retires. Record it; it is not a secret.
    jti: str = ""
    name: str
    subject: str  # the token's own `sa:` subject
    org: str
    namespaces: list[str]
    roles: list[str]
    ns_roles: dict[str, str] | None = None
    consumer: str = ""
    ttl_seconds: int | None  # None: never expires
    # The namespace the token names for its saves while it may write there: a list token's
    # recorded `saves_to`, or a `*` token's memory namespace before `*`; "" otherwise.
    saves_to: str = ""
    # Body fields this route ignores (`subject`, `is_service_account`).
    ignored_args: list[str] = Field(default_factory=list)
