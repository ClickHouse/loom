"""History-aware policy over the agent trace.

Role-based authorization is one role comparison per operation. Roles say
who may call what; they cannot say "only after a read", "no more than N an
hour", or "not after a logout". Those are statements about the caller's
own history, and `loom.agent_events` — keyed `(org, namespace, agent, ts)`,
carrying `kind` and `args` — is that history.

A rule is `effect` + `actions` + `condition`:

* `forbid`  — deny when the condition HOLDS ("more than 200 writes in 1h").
* `require` — deny when the condition does NOT hold ("a memory.get of the
  same key within 1h before a memory.delete").

Both layer on top of RBAC: a role check that fails never reaches here, and
a rule can only take permission away (deny overrides allow). Conditions
are three past-only temporal shapes, each compiled to ONE point query bound to
the caller's key:

* `formerly`     — some matching event within the window (∃).
* `count_within` — how many matching events within the window, compared.
* `not_since`    — an anchor happened within the window and the forbidden
                   event has not happened since the latest anchor.

plus `all` / `any` / `not` to combine them. `key` chooses the pin — the
slice of the trace a rule may see: `agent` (default; the row's sort-key
prefix, so the scan is one range), `session`, or `user`. A rule can never
read another principal's events: the pin is injected by the compiler, not
written by the author.

Evaluation is past-only and the current call is not yet in the trace when
its rule runs, so a witness is always an EARLIER event. Windows are closed
intervals measured back from the decision time and capped by
`policy_max_window_seconds`. Every leaf's value is returned with the
decision so a denial explains itself; every leaf error is reported, never
swallowed — the gate decides what an error means (fail-closed for writes).

This module is the rule grammar, the compiler and the verdict types, pure
with respect to storage: the compiler returns SQL + params.
`server/identity/app/history_policy.py` evaluates compiled leaves against any
object with `query_all` and holds the per-namespace rule cache.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

_WINDOW_RE = re.compile(r"^(\d{1,9})([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def window_seconds(text: str) -> int:
    """`1h` → 3600. Four units, integer amount, strictly positive."""
    m = _WINDOW_RE.match(text.strip())
    if not m:
        raise ValueError(f"window {text!r} must be <amount><unit> with unit in s/m/h/d (e.g. '1h')")
    secs = int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    if secs <= 0:
        raise ValueError(f"window {text!r} must be positive")
    return secs


def _max_window_seconds() -> int:
    from server.settings import settings

    return int(settings.policy_max_window_seconds)


# Validation context of a STORED rule being loaded: a window past the cap is
# clamped to it (and named in "clamped"), never a reason to drop the rule.
_LOAD_CONTEXT_KEY = "clamp_windows"


def _checked_window(v: str, info: ValidationInfo) -> str:
    """`v`, validated against policy_max_window_seconds. A new rule over the
    cap is refused. A stored rule over it (the cap was lowered after it was
    written) is enforced with the cap's window: dropping it would stop
    enforcing a forbid/require rule, the fail-open the gate must not have."""
    secs = window_seconds(v)
    cap = _max_window_seconds()
    if secs <= cap:
        return v.strip()
    ctx = info.context if isinstance(info.context, dict) else None
    if ctx is not None and ctx.get(_LOAD_CONTEXT_KEY):
        ctx.setdefault("clamped", []).append(f"{v.strip()}->{cap}s")
        return f"{cap}s"
    raise ValueError(f"window {v!r} ({secs}s) exceeds policy_max_window_seconds ({cap}s)")


# --------------------------------------------------------------------------
# Condition grammar
# --------------------------------------------------------------------------

KeyKind = Literal["agent", "session", "user"]
Cmp = Literal["gt", "gte", "lt", "lte", "eq"]

_ARG_REF = re.compile(r"^\$args\.([A-Za-z0-9_]+)$")


def _ops_list(v: str | list[str]) -> list[str]:
    ops = [v] if isinstance(v, str) else list(v)
    ops = [o.strip() for o in ops if o and o.strip()]
    if not ops:
        raise ValueError("operation must name at least one operation")
    if len(ops) > 32:
        raise ValueError("operation lists at most 32 operations")
    return ops


class _Leaf(BaseModel):
    within: str = Field(..., description="look-back window, e.g. '1h' (s/m/h/d)")
    operation: list[str] = Field(..., description="operation name(s) to match")
    key: KeyKind = "agent"
    success: bool | None = None
    # event args[k] must equal v; v is a literal or `$args.<field>` of the
    # current call (its projected args, event_args.project_args).
    args_equal: dict[str, str] = Field(default_factory=dict)

    @field_validator("operation", mode="before")
    @classmethod
    def _ops(cls, v: Any) -> list[str]:
        return _ops_list(v)

    @field_validator("within")
    @classmethod
    def _within(cls, v: str, info: ValidationInfo) -> str:
        return _checked_window(v, info)

    @field_validator("args_equal")
    @classmethod
    def _args_equal(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 8:
            raise ValueError("args_equal compares at most 8 fields")
        for k in v:
            if not re.match(r"^[A-Za-z0-9_]{1,64}$", k):
                raise ValueError(f"args_equal key {k!r} is not a field name")
        return v


class Formerly(_Leaf):
    op: Literal["formerly"]


class CountWithin(_Leaf):
    op: Literal["count_within"]
    cmp: Cmp
    value: int = Field(..., ge=0, le=10_000_000)


class BreadthWithin(BaseModel):
    """How many DISTINCT memory keys the pinned principal has been shown in
    the window — a memory-extraction metric per principal, read from loom.memory_exposures
    rather than the event trace. `value` compares the count; `fraction` compares count /
    live rows in the namespace (exactly one of the two). `key` defaults to `actor`, the
    verified token subject — the one pin the caller cannot choose. `operation` narrows to
    the read ops that count (empty = every recorded read)."""

    op: Literal["breadth_within"]
    within: str = Field(..., description="look-back window, e.g. '1h' (s/m/h/d)")
    key: Literal["actor", "agent", "session", "user"] = "actor"
    operation: list[str] = Field(default_factory=list)
    cmp: Cmp
    value: int | None = Field(default=None, ge=0, le=10_000_000)
    fraction: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("within")
    @classmethod
    def _within(cls, v: str, info: ValidationInfo) -> str:
        return _checked_window(v, info)

    @field_validator("operation", mode="before")
    @classmethod
    def _ops(cls, v: Any) -> list[str]:
        if v is None or v == [] or v == "":
            return []
        return _ops_list(v)

    @model_validator(mode="after")
    def _one_of(self) -> BreadthWithin:
        if (self.fraction is None) == (self.value is None):
            raise ValueError("breadth_within takes exactly one of `value` or `fraction`")
        return self


class NotSince(_Leaf):
    """`operation` is the event that must NOT have happened since the latest
    `anchor` within the window. Holds iff an anchor exists in the window and
    no `operation` event is later than the latest anchor."""

    op: Literal["not_since"]
    anchor: list[str]

    @field_validator("anchor", mode="before")
    @classmethod
    def _anchor(cls, v: Any) -> list[str]:
        return _ops_list(v)


class All(BaseModel):
    op: Literal["all"]
    of: list[Condition] = Field(..., min_length=1, max_length=8)


class AnyOf(BaseModel):
    op: Literal["any"]
    of: list[Condition] = Field(..., min_length=1, max_length=8)


class Not(BaseModel):
    op: Literal["not"]
    of: Condition


Condition = Annotated[
    Formerly | CountWithin | NotSince | BreadthWithin | All | AnyOf | Not,
    Field(discriminator="op"),
]

All.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()

Effect = Literal["forbid", "require"]
# Per-rule enforcement: follow the deployment, record only, or block on its own.
RuleMode = Literal["inherit", "shadow", "enforce"]

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_*]*|\.\*)*$|^\*$")


class PolicyRule(BaseModel):
    policy_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    effect: Effect
    # operation names or globs: 'memory.delete', 'memory.*', '*'
    actions: list[str] = Field(..., min_length=1, max_length=64)
    condition: Condition
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    mode: RuleMode = "inherit"

    def effective_mode(self, deployment_mode: str) -> str:
        """`off` at the deployment wins outright; otherwise the rule's own
        mode overrides (a rule that only logs under an enforcing deployment, and its converse: a
        rule that graduates to enforce while the deployment still shadows)."""
        if deployment_mode == "off":
            return "off"
        return deployment_mode if self.mode == "inherit" else self.mode

    @field_validator("actions")
    @classmethod
    def _actions(cls, v: list[str]) -> list[str]:
        out = []
        for a in v:
            a = a.strip()
            if not _ACTION_RE.match(a):
                raise ValueError(f"action {a!r} is not an operation name or glob")
            out.append(a)
        return out

    def applies_to(self, operation: str) -> bool:
        return any(fnmatch.fnmatchcase(operation, a) for a in self.actions)

    def leaf_count(self) -> int:
        return _count_leaves(self.condition)


def _count_leaves(c: Any) -> int:
    if isinstance(c, (Formerly, CountWithin, NotSince, BreadthWithin)):
        return 1
    if isinstance(c, (All, AnyOf)):
        return sum(_count_leaves(x) for x in c.of)
    if isinstance(c, Not):
        return _count_leaves(c.of)
    return 0


MAX_LEAVES_PER_RULE = 8


def parse_rule(raw: dict[str, Any], *, stored: bool = False) -> PolicyRule:
    """A rule from its wire/storage shape. `stored`: a row already accepted
    once — a window over a since-lowered cap is clamped (one WARNING)
    instead of refused."""
    ctx: dict[str, Any] = {_LOAD_CONTEXT_KEY: True} if stored else {}
    rule = PolicyRule.model_validate(raw, context=ctx)
    if ctx.get("clamped"):
        log.warning(
            "policy %s: window(s) %s exceed policy_max_window_seconds; enforced at the cap",
            raw.get("policy_id"),
            ", ".join(ctx["clamped"]),
        )
    if rule.leaf_count() > MAX_LEAVES_PER_RULE:
        raise ValueError(f"a rule evaluates at most {MAX_LEAVES_PER_RULE} history leaves")
    return rule


# --------------------------------------------------------------------------
# The call being decided
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallContext:
    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    operation: str
    args: dict[str, str] = field(default_factory=dict)
    now: datetime | None = None
    # Verified token subject ('' when auth is off). Only the exposure-ledger
    # leaf pins on it: loom.agent_events has no actor column.
    actor: str = ""

    def key_clause(self, key: KeyKind) -> tuple[str, dict[str, Any]]:
        """The pin: which slice of the trace this leaf may see."""
        clause = "org = {p_org:String} AND namespace = {p_ns:String} AND agent = {p_agent:String}"
        params: dict[str, Any] = {
            "p_org": self.org,
            "p_ns": self.namespace,
            "p_agent": self.agent,
        }
        if key == "session":
            clause += " AND session_id = {p_session:String}"
            params["p_session"] = self.session_id
        elif key == "user":
            clause += " AND user_id = {p_user:String}"
            params["p_user"] = self.user_id
        return clause, params

    def decision_time(self) -> datetime:
        return self.now or datetime.now(UTC)


# Rows written by background passes are never a principal's own history.
# A row with kind='' is told apart by its operation prefix.
_NOT_WORKER = "kind != 'worker' AND NOT startsWith(operation, 'worker.')"


@dataclass(frozen=True)
class CompiledLeaf:
    sql: str
    params: dict[str, Any]
    kind: str  # formerly | count_within | not_since


class RuleCompileError(ValueError):
    pass


def _resolve_arg(template: str, ctx: CallContext) -> str | None:
    m = _ARG_REF.match(template)
    if not m:
        return template
    return ctx.args.get(m.group(1))


def compile_leaf(leaf: Formerly | CountWithin | NotSince, ctx: CallContext) -> CompiledLeaf | None:
    """One point query per leaf, bound to the caller's key. Returns None when
    an `$args.<field>` reference has no value on this call: nothing to
    correlate on, so the leaf cannot hold (and a `require` on it denies)."""
    key_clause, params = ctx.key_clause(leaf.key)
    secs = window_seconds(leaf.within)
    now = ctx.decision_time()
    params.update({"p_now": now, "p_secs": secs})
    where = [
        key_clause,
        _NOT_WORKER,
        "ts <= {p_now:DateTime64(3)}",
        "ts >= {p_now:DateTime64(3)} - INTERVAL {p_secs:UInt32} SECOND",
    ]
    if leaf.success is not None:
        where.append("success = {p_success:Bool}")
        params["p_success"] = bool(leaf.success)
    for i, (k, template) in enumerate(sorted(leaf.args_equal.items())):
        value = _resolve_arg(template, ctx)
        if value is None:
            return None
        where.append(f"args[{{p_ak{i}:String}}] = {{p_av{i}:String}}")
        params[f"p_ak{i}"] = k
        params[f"p_av{i}"] = value

    if isinstance(leaf, NotSince):
        params["p_left"] = list(leaf.operation)
        params["p_anchor"] = list(leaf.anchor)
        where.append(
            "(operation IN {p_left:Array(String)} OR operation IN {p_anchor:Array(String)})"
        )
        sql = (
            "SELECT maxOrNullIf(ts, operation IN {p_anchor:Array(String)}) AS anchor_ts, "
            "maxOrNullIf(ts, operation IN {p_left:Array(String)}) AS left_ts "
            "FROM loom.agent_events WHERE " + " AND ".join(where)
        )
        return CompiledLeaf(sql=sql, params=params, kind="not_since")

    params["p_ops"] = list(leaf.operation)
    where.append("operation IN {p_ops:Array(String)}")
    sql = "SELECT count() AS n FROM loom.agent_events WHERE " + " AND ".join(where)
    return CompiledLeaf(sql=sql, params=params, kind=leaf.op)


EXPOSURES_TABLE = "loom.memory_exposures"


def compile_breadth(leaf: BreadthWithin, ctx: CallContext) -> CompiledLeaf | None:
    """uniqExact over the returned keys the pinned principal was shown in the
    window, plus the namespace's live row count for the fraction form. One
    range read on (org, namespace, actor, ts) when the pin is the actor.
    Returns None when the pin is the actor and the call carries none (auth
    off): nothing to bound, so the leaf cannot hold."""
    secs = window_seconds(leaf.within)
    now = ctx.decision_time()
    params: dict[str, Any] = {
        "p_org": ctx.org,
        "p_ns": ctx.namespace,
        "p_now": now,
        "p_secs": secs,
    }
    where = [
        "org = {p_org:String}",
        "namespace = {p_ns:String}",
        "ts <= {p_now:DateTime64(3)}",
        "ts >= {p_now:DateTime64(3)} - INTERVAL {p_secs:UInt32} SECOND",
    ]
    if leaf.key == "actor":
        if not ctx.actor:
            return None
        where.append("actor = {p_actor:String}")
        params["p_actor"] = ctx.actor
    elif leaf.key == "agent":
        where.append("agent = {p_agent:String}")
        params["p_agent"] = ctx.agent
    elif leaf.key == "session":
        where.append("session_id = {p_session:String}")
        params["p_session"] = ctx.session_id
    else:
        where.append("user_id = {p_user:String}")
        params["p_user"] = ctx.user_id
    if leaf.operation:
        where.append("operation IN {p_ops:Array(String)}")
        params["p_ops"] = list(leaf.operation)
    sql = (
        "SELECT uniqExact(k) AS n, "
        "(SELECT count() FROM loom.memory FINAL WHERE org = {p_org:String} "
        "AND namespace = {p_ns:String} AND tombstoned_at IS NULL) AS total "
        f"FROM {EXPOSURES_TABLE} ARRAY JOIN returned_keys AS k WHERE " + " AND ".join(where)
    )
    return CompiledLeaf(sql=sql, params=params, kind="breadth_within")


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


@dataclass
class LeafResult:
    op: str
    holds: bool
    value: Any = None
    error: str = ""
    latency_ms: int = 0
    skipped: str = ""


@dataclass
class RuleVerdict:
    policy_id: str
    effect: str
    holds: bool | None  # None when a leaf errored
    denies: bool
    mode: str = "inherit"  # effective mode this verdict was evaluated under
    leaves: list[LeafResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class PolicyDecision:
    decision: Literal["allow", "deny"]
    determining_rules: list[str]
    errors: list[str]
    verdicts: list[RuleVerdict]
    latency_ms: int = 0
    # Denying rules whose effective mode is `enforce` — the ones that block.
    enforcing_rules: list[str] = field(default_factory=list)
    # Errors raised by rules whose effective mode is `enforce`.
    enforcing_errors: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return bool(self.verdicts)

    def to_detail(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "determining_rules": list(self.determining_rules),
            "enforcing_rules": list(self.enforcing_rules),
            "errors": list(self.errors),
            "rules": [
                {
                    "policy_id": v.policy_id,
                    "effect": v.effect,
                    "mode": v.mode,
                    "holds": v.holds,
                    "denies": v.denies,
                    "leaves": [
                        {
                            "op": lf.op,
                            "holds": lf.holds,
                            "value": lf.value,
                            "error": lf.error,
                            "skipped": lf.skipped,
                        }
                        for lf in v.leaves
                    ],
                }
                for v in self.verdicts
            ],
        }


__all__ = [
    "All",
    "AnyOf",
    "BreadthWithin",
    "CallContext",
    "CompiledLeaf",
    "Condition",
    "CountWithin",
    "Formerly",
    "LeafResult",
    "Not",
    "NotSince",
    "PolicyDecision",
    "PolicyRule",
    "RuleMode",
    "RuleVerdict",
    "compile_breadth",
    "compile_leaf",
    "parse_rule",
    "window_seconds",
]
