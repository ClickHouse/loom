"""Protected history is UNKNOWN before outcomes or negation are evaluated."""

from __future__ import annotations

from datetime import datetime

import pytest

from server.identity.app import history_policy
from server.identity.domain import policy_rules as rules

_REASON = "protected history unavailable"
_NOW = datetime(2030, 1, 1, 12)


def context():
    return rules.CallContext(
        org="acme",
        namespace="docs",
        agent="reader",
        user_id="person",
        session_id="session",
        operation="memory.get",
        actor="subject",
        now=_NOW,
    )


def condition(kind, negate):
    if kind == "breadth_within":
        leaf = dict(op=kind, within="1h", key="actor", cmp="lt", value=2)
    else:
        leaf = dict(
            op=kind,
            within="1h",
            operation=["knowledge.search"],
            key="user",
            success=True,
            args_equal={"source_uri": "requested-document"},
        )
        if kind == "count_within":
            leaf.update(cmp="lt", value=2)
        elif kind == "not_since":
            leaf["anchor"] = ["memory.set"]
    return dict(op="not", of=leaf) if negate else leaf


class HistoryRows:
    def __init__(self, protected, *, alternative=False):
        self.protected = protected
        self.alternative = alternative
        self.calls = []

    async def query_all(self, sql, params):
        self.calls.append((sql, dict(params)))
        return [
            dict(
                protected_history=self.protected,
                n=999 if self.alternative else 1,
                total=1000 if self.alternative else 2,
                anchor_ts=None if self.alternative else _NOW,
                left_ts=_NOW if self.alternative else None,
            )
        ]


@pytest.mark.parametrize("kind", ["formerly", "count_within", "not_since", "breadth_within"])
@pytest.mark.parametrize("effect", ["require", "forbid"])
@pytest.mark.parametrize("negate", [False, True])
async def test_protected_history_never_exposes_a_value_or_becomes_true_under_negation(
    kind, effect, negate
):
    rule = rules.PolicyRule(
        policy_id="guard", effect=effect, actions=["memory.get"], condition=condition(kind, negate)
    )
    for alternative in (False, True):
        ch = HistoryRows(True, alternative=alternative)
        result = await history_policy.evaluate_rule(rule, context(), ch)
        assert result.holds is None
        assert result.denies is False
        assert result.errors == [_REASON]
        assert len(result.leaves) == 1
        assert result.leaves[0].value is None and result.leaves[0].error == _REASON
        assert len(ch.calls) == 1


@pytest.mark.parametrize("kind", ["formerly", "count_within", "not_since", "breadth_within"])
@pytest.mark.parametrize("effect", ["require", "forbid"])
@pytest.mark.parametrize("negate", [False, True])
async def test_unmarked_history_retains_ordinary_predicates(kind, effect, negate):
    rule = rules.PolicyRule(
        policy_id="guard", effect=effect, actions=["memory.get"], condition=condition(kind, negate)
    )
    ch = HistoryRows(False)
    result = await history_policy.evaluate_rule(rule, context(), ch)
    assert result.holds is (not negate)
    assert result.denies is ((not negate) if effect == "forbid" else negate)
    assert result.errors == [] and result.leaves[0].error == ""
    assert result.leaves[0].value is not None
    assert len(ch.calls) == 1
