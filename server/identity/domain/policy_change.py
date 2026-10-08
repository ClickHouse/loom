"""Does a rule change widen what an agent may do?

A policy update that only NARROWS an agent's privileges is safe on its own; one
that EXPANDS them deserves a human's attention. Loom cannot block a widening — `policy.set`
is already ADMIN — but it can name it, so the audit trail and an alert can
tell "tightened the cap" from "raised the cap 10×" without reading JSON diffs.

The classifier is deliberately conservative: when it cannot tell, it says
`widening=True` with the reason `unknown`. Rules that only ever narrow read
as quiet; anything else is loud. Pure, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.identity.domain.policy_rules import (
    All,
    AnyOf,
    CountWithin,
    Formerly,
    Not,
    NotSince,
    PolicyRule,
    window_seconds,
)

_MODE_RANK = {"shadow": 0, "inherit": 1, "enforce": 2}


@dataclass
class ChangeVerdict:
    widening: bool
    reasons: list[str] = field(default_factory=list)

    def to_list(self) -> list[str]:
        return list(self.reasons)


def _leaf_pairs(cond: Any) -> list[Any]:
    """Leaves in traversal order (combinators flattened)."""
    if isinstance(cond, (Formerly, CountWithin, NotSince)):
        return [cond]
    if isinstance(cond, (All, AnyOf)):
        return [x for c in cond.of for x in _leaf_pairs(c)]
    if isinstance(cond, Not):
        return _leaf_pairs(cond.of)
    return []


def _shape(cond: Any) -> Any:
    """Structural skeleton without windows/values: same shape ⇒ comparable."""
    if isinstance(cond, (All, AnyOf)):
        return (cond.op, tuple(_shape(c) for c in cond.of))
    if isinstance(cond, Not):
        return ("not", _shape(cond.of))
    if isinstance(cond, CountWithin):
        return (
            "count_within",
            tuple(cond.operation),
            cond.key,
            cond.cmp,
            tuple(sorted(cond.args_equal)),
        )
    if isinstance(cond, Formerly):
        return ("formerly", tuple(cond.operation), cond.key, tuple(sorted(cond.args_equal)))
    if isinstance(cond, NotSince):
        return (
            "not_since",
            tuple(cond.operation),
            tuple(cond.anchor),
            cond.key,
            tuple(sorted(cond.args_equal)),
        )
    return ("?",)


def _count_widened(effect: str, old: CountWithin, new: CountWithin) -> bool | None:
    """For a `forbid count >= N`, raising N widens; for `require count >= N`,
    lowering N widens. Returns None when the comparison direction is unclear."""
    if old.cmp != new.cmp:
        return None
    upper = old.cmp in ("gt", "gte")  # condition holds when count is LARGE
    if old.value == new.value:
        return False
    raised = new.value > old.value
    if effect == "forbid":
        # forbid fires when condition holds; a harder-to-reach condition = widening
        return raised if upper else not raised
    # require: condition must hold; an easier condition = widening
    return (not raised) if upper else raised


def _window_widened(effect: str, kind: str, old_w: str, new_w: str) -> bool | None:
    o, n = window_seconds(old_w), window_seconds(new_w)
    if o == n:
        return False
    longer = n > o
    if kind == "count_within":
        # a longer window makes a forbid-count easier to hit (narrowing) and a
        # require-count easier to satisfy (widening)
        return longer if effect == "require" else not longer
    # formerly / not_since: a longer window admits more witnesses — for a
    # require that is widening, for a forbid it is narrowing
    return longer if effect == "require" else not longer


def classify_change(old: PolicyRule | None, new: PolicyRule | None) -> ChangeVerdict:
    """Compare two versions of a rule (None = absent). Conservative."""
    if old is None and new is None:
        return ChangeVerdict(False, [])
    if new is None:
        if old is not None and not old.enabled:
            return ChangeVerdict(False, ["deleted a disabled rule"])
        return ChangeVerdict(True, ["rule deleted"])
    if old is None:
        if not new.enabled:
            return ChangeVerdict(True, ["created disabled (inert)"])
        return ChangeVerdict(False, ["rule created"])

    reasons: list[str] = []
    widening = False
    if old.enabled and not new.enabled:
        widening = True
        reasons.append("disabled")
    elif not old.enabled and new.enabled:
        reasons.append("enabled")
    if _MODE_RANK[new.mode] < _MODE_RANK[old.mode]:
        widening = True
        reasons.append(f"mode {old.mode} -> {new.mode}")
    elif new.mode != old.mode:
        reasons.append(f"mode {old.mode} -> {new.mode}")
    if old.effect != new.effect:
        widening = True
        reasons.append(f"effect {old.effect} -> {new.effect} (unknown)")
    dropped = set(old.actions) - set(new.actions)
    if dropped:
        widening = True
        reasons.append(f"actions no longer covered: {', '.join(sorted(dropped))}")
    added = set(new.actions) - set(old.actions)
    if added:
        reasons.append(f"actions added: {', '.join(sorted(added))}")

    if _shape(old.condition) != _shape(new.condition):
        widening = True
        reasons.append("condition restructured (unknown)")
        return ChangeVerdict(widening, reasons)

    for o, n in zip(_leaf_pairs(old.condition), _leaf_pairs(new.condition), strict=True):
        w = _window_widened(new.effect, n.op, o.within, n.within)
        if w is None:
            widening = True
            reasons.append(f"{n.op} window changed (unknown)")
        elif w:
            widening = True
            reasons.append(f"{n.op} window {o.within} -> {n.within} (widens)")
        elif o.within != n.within:
            reasons.append(f"{n.op} window {o.within} -> {n.within}")
        if isinstance(o, CountWithin) and isinstance(n, CountWithin):
            c = _count_widened(new.effect, o, n)
            if c is None:
                widening = True
                reasons.append("count comparison changed (unknown)")
            elif c:
                widening = True
                reasons.append(f"count threshold {o.value} -> {n.value} (widens)")
            elif o.value != n.value:
                reasons.append(f"count threshold {o.value} -> {n.value}")
        if o.success != n.success:
            widening = True
            reasons.append("success filter changed (unknown)")
        if dict(o.args_equal) != dict(n.args_equal):
            # fewer correlations = more witnesses = widening for require
            if set(n.args_equal) < set(o.args_equal) and new.effect == "require":
                widening = True
                reasons.append("dropped an args correlation (widens)")
            else:
                widening = True
                reasons.append("args correlation changed (unknown)")
    return ChangeVerdict(widening, reasons)


__all__ = ["ChangeVerdict", "classify_change"]
