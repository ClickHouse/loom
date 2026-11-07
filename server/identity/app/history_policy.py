"""History-policy evaluation: each compiled leaf of a rule runs as one point
query against the caller's slice of the trace, and the verdicts combine into
the call's decision. The rule grammar and compiler are
`server/identity/domain/policy_rules.py`; the rules of a namespace are cached
here per process, with a TTL and explicit invalidation.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from server.core.errors import caller_text
from server.identity.domain.policy_rules import (
    All,
    AnyOf,
    BreadthWithin,
    CallContext,
    Cmp,
    CountWithin,
    Formerly,
    LeafResult,
    Not,
    NotSince,
    PolicyDecision,
    PolicyRule,
    RuleCompileError,
    RuleVerdict,
    compile_breadth,
    compile_leaf,
)

log = logging.getLogger(__name__)


def _compare(n: int, cmp: Cmp, value: int) -> bool:
    return {
        "gt": n > value,
        "gte": n >= value,
        "lt": n < value,
        "lte": n <= value,
        "eq": n == value,
    }[cmp]


class _LeafError(Exception):
    pass


def _compare_float(x: float, cmp: Cmp, value: float) -> bool:
    return {
        "gt": x > value,
        "gte": x >= value,
        "lt": x < value,
        "lte": x <= value,
        "eq": abs(x - value) < 1e-9,
    }[cmp]


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


async def _eval_leaf(leaf: Any, ctx: CallContext, ch: Any, out: list[LeafResult]) -> bool:
    started = time.perf_counter()
    if isinstance(leaf, BreadthWithin):
        compiled = compile_breadth(leaf, ctx)
        if compiled is None:
            out.append(LeafResult(op=leaf.op, holds=False, skipped="no actor on this call"))
            return False
    else:
        compiled = compile_leaf(leaf, ctx)
    if compiled is None:
        out.append(LeafResult(op=leaf.op, holds=False, skipped="args reference unresolved"))
        return False
    try:
        rows = await ch.query_all(compiled.sql, compiled.params)
    except Exception as e:  # a failed leaf is reported on the decision, then raised
        log.warning("history policy leaf %s failed: %s", leaf.op, e)
        msg = f"{leaf.op}: {caller_text(e)}"[:400]  # the text stays in the log
        out.append(
            LeafResult(
                op=leaf.op,
                holds=False,
                error=msg,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        raise _LeafError(msg) from e
    row = rows[0] if rows else {}
    if compiled.kind == "not_since":
        anchor_ts = row.get("anchor_ts")
        left_ts = row.get("left_ts")
        holds = anchor_ts is not None and (left_ts is None or left_ts <= anchor_ts)
        value: Any = {"anchor_ts": _iso(anchor_ts), "left_ts": _iso(left_ts)}
    elif compiled.kind == "breadth_within":
        n = int(row.get("n", 0) or 0)
        total = int(row.get("total", 0) or 0)
        value = {"distinct_keys": n, "namespace_rows": total}
        if leaf.value is not None:
            holds = _compare(n, leaf.cmp, leaf.value)
        else:
            frac = (n / total) if total else 0.0
            value["fraction"] = round(frac, 4)
            holds = _compare_float(frac, leaf.cmp, float(leaf.fraction or 0.0))
    else:
        n = int(row.get("n", 0) or 0)
        value = n
        holds = n > 0 if compiled.kind == "formerly" else _compare(n, leaf.cmp, leaf.value)
    out.append(
        LeafResult(
            op=leaf.op,
            holds=holds,
            value=value,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    return holds


async def _eval_condition(cond: Any, ctx: CallContext, ch: Any, out: list[LeafResult]) -> bool:
    if isinstance(cond, (Formerly, CountWithin, NotSince, BreadthWithin)):
        return await _eval_leaf(cond, ctx, ch, out)
    if isinstance(cond, All):
        for c in cond.of:
            if not await _eval_condition(c, ctx, ch, out):
                return False
        return True
    if isinstance(cond, AnyOf):
        for c in cond.of:
            if await _eval_condition(c, ctx, ch, out):
                return True
        return False
    if isinstance(cond, Not):
        return not await _eval_condition(cond.of, ctx, ch, out)
    raise RuleCompileError(f"unknown condition {type(cond).__name__}")


async def evaluate_rule(
    rule: PolicyRule, ctx: CallContext, ch: Any, deployment_mode: str = "enforce"
) -> RuleVerdict:
    leaves: list[LeafResult] = []
    mode = rule.effective_mode(deployment_mode)
    try:
        holds = await _eval_condition(rule.condition, ctx, ch, leaves)
    except _LeafError as e:
        return RuleVerdict(
            policy_id=rule.policy_id,
            effect=rule.effect,
            holds=None,
            denies=False,
            mode=mode,
            leaves=leaves,
            errors=[str(e)],
        )
    denies = holds if rule.effect == "forbid" else not holds
    return RuleVerdict(
        policy_id=rule.policy_id,
        effect=rule.effect,
        holds=holds,
        denies=denies,
        mode=mode,
        leaves=leaves,
    )


async def evaluate(
    rules: list[PolicyRule], ctx: CallContext, ch: Any, deployment_mode: str = "enforce"
) -> PolicyDecision:
    """Evaluate every enabled rule that applies to the call. Deny-overrides:
    one denying rule denies. A rule whose leaf errored neither allows nor
    denies; its error is reported for the gate to interpret. `enforcing_rules`
    / `enforcing_errors` are the subset from rules whose effective mode is
    `enforce` — the gate blocks on those alone; the rest is ledger evidence."""
    started = time.perf_counter()
    verdicts: list[RuleVerdict] = []
    for rule in rules:
        if not rule.enabled or not rule.applies_to(ctx.operation):
            continue
        verdicts.append(await evaluate_rule(rule, ctx, ch, deployment_mode))
    determining = [v.policy_id for v in verdicts if v.denies]
    errors = [e for v in verdicts for e in v.errors]
    return PolicyDecision(
        decision="deny" if determining else "allow",
        determining_rules=determining,
        errors=errors,
        verdicts=verdicts,
        latency_ms=int((time.perf_counter() - started) * 1000),
        enforcing_rules=[v.policy_id for v in verdicts if v.denies and v.mode == "enforce"],
        enforcing_errors=[e for v in verdicts if v.mode == "enforce" for e in v.errors],
    )


# --------------------------------------------------------------------------
# Rule cache — per (org, namespace), TTL + explicit invalidation
# --------------------------------------------------------------------------

_cache: dict[tuple[str, str], tuple[float, list[PolicyRule]]] = {}


def invalidate(org: str | None = None, namespace: str | None = None) -> None:
    if org is None:
        _cache.clear()
        return
    _cache.pop((org, namespace or ""), None)


async def rules_for(org: str, namespace: str, ch: Any | None = None) -> list[PolicyRule]:
    from server.identity.store import policy as policy_repo
    from server.settings import settings

    now = time.monotonic()
    hit = _cache.get((org, namespace))
    if hit and (now - hit[0]) < float(settings.policy_cache_ttl_seconds):
        return hit[1]
    from server.core.ch import sequential_reads

    with sequential_reads():  # a rule another replica just stored
        rows = await policy_repo.list_rules(org, namespace, ch=ch)
    rules: list[PolicyRule] = []
    for r in rows:
        try:
            rules.append(policy_repo.row_to_rule(r))
        except Exception as e:  # noqa: BLE001 — a bad stored rule must not take down the rest
            log.warning(
                "policy %s in %s/%s no longer parses and is not enforced: %s",
                r.get("policy_id"),
                org,
                namespace,
                str(e)[:200],
            )
            continue
    _cache[(org, namespace)] = (now, rules)
    return rules


__all__ = [
    "evaluate",
    "evaluate_rule",
    "invalidate",
    "rules_for",
]
