"""History-policy wire models — set / list / delete / check / decisions.

`policy.set` and `policy.delete` are ADMIN: a rule takes permission away
from every writer in the namespace. `policy.check` is the dry run — the
same evaluator the gate runs, against a hypothetical call, with per-leaf
values — so an operator (or an agent) can see what a rule would do before
`LOOM_POLICY_MODE` leaves `shadow`. `policy.decisions` reads the ledger.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class PolicySetInput(BaseModel):
    org: str
    namespace: str
    # The rule's id in the namespace (letters, digits, `_.:-`); an existing id is
    # replaced.
    policy_id: str = Field(..., min_length=1, max_length=128)
    # `forbid` denies the call when the condition holds; `require` denies it when the
    # condition does not hold.
    effect: Literal["forbid", "require"]
    actions: list[str] = Field(
        ...,
        min_length=1,
        max_length=64,
        description="operation names or globs this rule applies to ('memory.delete', 'memory.*')",
    )
    condition: dict[str, Any] = Field(
        ...,
        description=(
            "history condition: {op: formerly|count_within|not_since|all|any|not, ...}; "
            "leaves take within ('1h'), operation, key (agent|session|user), success, args_equal"
        ),
    )
    # Free text stored with the rule.
    description: str = Field(default="", max_length=2000)
    # False keeps the rule stored but skips it at evaluation.
    enabled: bool = True
    # Per-rule enforcement: inherit the deployment's
    # LOOM_POLICY_MODE, or override — shadow (record only, even when the
    # deployment enforces) / enforce (block, even when it only shadows).
    mode: Literal["inherit", "shadow", "enforce"] = "inherit"
    # Author recorded only when the call has no authenticated principal to stamp.
    created_by: str = ""


class PolicyRuleOut(BaseModel):
    policy_id: str
    effect: str
    actions: list[str]
    condition: dict[str, Any]
    description: str
    enabled: bool
    mode: str = "inherit"
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PolicySetOutput(BaseModel):
    # The rule as stored.
    rule: PolicyRuleOut
    # How many history leaves the condition has; each is one query per evaluation.
    leaf_count: int
    # Did this change WIDEN what agents may do (raise a cap, drop a
    # correlation, disable, downgrade mode)? Narrowing is quiet; widening
    # also writes a `policy.widened` audit row (widening is the change worth auditing).
    widening: bool = False
    # What changed against the stored version, one phrase per difference
    # (`rule created` for a new id; `(widens)` / `(unknown)` mark widening ones).
    change: list[str] = []


class PolicyListInput(BaseModel):
    org: str
    namespace: str
    # False leaves out rules with `enabled` false; deleted rules never appear.
    include_disabled: bool = True


class PolicyListOutput(BaseModel):
    # The namespace's live rules, ordered by `policy_id`.
    rules: list[PolicyRuleOut]
    # The deployment's `LOOM_POLICY_MODE`: `off`, `shadow` or `enforce`.
    mode: str


class PolicyDeleteInput(BaseModel):
    org: str
    namespace: str
    # The rule to remove; it is tombstoned, so past decisions naming it stay readable.
    policy_id: str
    # Written as the rule's `created_by` only when the stored row has none; no
    # remover is recorded otherwise.
    removed_by: str = ""


class PolicyDeleteOutput(BaseModel):
    # False when no live rule has that id.
    removed: bool
    # True when deleting it widened what agents may do (an enabled rule); a
    # `policy.widened` audit row is written then.
    widening: bool = False


class PolicyCheckInput(BaseModel):
    """A hypothetical call: what would the rules decide right now?"""

    org: str
    namespace: str
    operation: str = Field(..., description="the operation the call would invoke")
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="the call's arguments (projected the same way agent_events.args is)",
    )
    at: datetime | None = Field(
        default=None, description="decision time; default now (past-only, closed windows)"
    )
    # Evaluate these rules instead of the stored ones — validate a rule before
    # storing it, or replay a stored trace against a candidate rule.
    rules: list[dict[str, Any]] | None = None


class PolicyCheckOutput(BaseModel):
    # `deny` when a rule denies the call, or a rule errored on a write-shaped
    # operation; `allow` otherwise.
    decision: Literal["allow", "deny"]
    # The same verdict as a boolean: true when an enforcing gate would block the call.
    would_enforce: bool
    # The deployment's `LOOM_POLICY_MODE`; the dry run evaluates as if every rule enforced.
    mode: str
    # Whether the operation writes (a rule error then fails closed); null when the
    # operation name is unknown.
    write_shaped: bool | None
    # Ids of the rules that deny the call; empty when none does.
    determining_rules: list[str]
    # Leaf evaluation errors; a rule that errored neither allows nor denies.
    errors: list[str]
    # Per applicable rule: effect, mode, whether the condition held, whether it denies,
    # and each leaf's value.
    rules: list[dict[str, Any]]
    # Milliseconds the evaluation took.
    latency_ms: int


class PolicyDecisionsInput(BaseModel):
    org: str
    namespace: str
    # Only decisions with this verdict; None returns both.
    decision: Literal["allow", "deny"] | None = None
    # At most this many rows, 1-1000, newest first.
    limit: int = Field(default=100, ge=1, le=1000)


class PolicyDecisionRow(BaseModel):
    ts: datetime
    agent: str
    user_id: str
    session_id: str
    operation: str
    request_id: str
    mode: str
    decision: str
    enforced: bool
    determining_rules: list[str]
    # Why the gate or a leaf could not answer: `policy gate: <error>` or `<op>: <error>`,
    # `<error>` the exception's kind or a Loom error's `<CODE>: <message>`.
    errors: list[str]
    latency_ms: int


class PolicyDecisionsOutput(BaseModel):
    # Recorded gate decisions for the namespace, newest first.
    rows: list[PolicyDecisionRow]
