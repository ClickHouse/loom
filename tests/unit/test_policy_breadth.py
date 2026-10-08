"""breadth_within: a policy leaf over the exposure ledger —
how many distinct memory keys the pinned principal has been shown."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from server.identity.app import history_policy as pol
from server.identity.domain import policy_rules as rules


def _ctx(**kw) -> rules.CallContext:
    base = dict(
        org="acme",
        namespace="shop",
        agent="webshop",
        user_id="-",
        session_id="-",
        operation="memory.search",
        actor="sa:shop-bot",
        now=datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
    )
    base.update(kw)
    return rules.CallContext(**base)


class _CH:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def query_all(self, sql, params):
        self.calls.append((sql, params))
        return self.rows


def test_rule_parses_and_counts_as_one_leaf():
    rule = rules.parse_rule(
        {
            "policy_id": "breadth-5pct",
            "effect": "forbid",
            "actions": ["memory.*"],
            "condition": {"op": "breadth_within", "within": "1h", "cmp": "gt", "fraction": 0.05},
        }
    )
    assert rule.leaf_count() == 1
    assert rule.condition.key == "actor" and rule.condition.operation == []


def test_exactly_one_of_value_or_fraction():
    with pytest.raises(ValueError):
        rules.BreadthWithin(op="breadth_within", within="1h", cmp="gt")
    with pytest.raises(ValueError):
        rules.BreadthWithin(op="breadth_within", within="1h", cmp="gt", value=5, fraction=0.1)


def test_compiles_to_uniqexact_over_the_ledger_pinned_on_the_actor():
    leaf = rules.BreadthWithin(
        op="breadth_within",
        within="1h",
        cmp="gt",
        value=200,
        operation=["memory.search", "memory.list"],
    )
    c = rules.compile_breadth(leaf, _ctx())
    assert c is not None and c.kind == "breadth_within"
    assert "uniqExact(k)" in c.sql and "ARRAY JOIN returned_keys AS k" in c.sql
    assert rules.EXPOSURES_TABLE in c.sql
    assert "actor = {p_actor:String}" in c.sql and c.params["p_actor"] == "sa:shop-bot"
    assert "operation IN {p_ops:Array(String)}" in c.sql
    assert c.params["p_ops"] == ["memory.search", "memory.list"]
    assert "tombstoned_at IS NULL" in c.sql  # the fraction denominator
    assert c.params["p_secs"] == 3600


def test_other_pins_use_the_ledger_identity_columns():
    for key, col in (
        ("agent", "agent = {p_agent:String}"),
        ("session", "session_id = {p_session:String}"),
        ("user", "user_id = {p_user:String}"),
    ):
        leaf = rules.BreadthWithin(op="breadth_within", within="1h", cmp="gt", value=1, key=key)
        c = rules.compile_breadth(leaf, _ctx())
        assert c is not None and col in c.sql and "actor =" not in c.sql


def test_actor_pin_without_a_token_cannot_hold():
    leaf = rules.BreadthWithin(op="breadth_within", within="1h", cmp="gt", value=1)
    assert rules.compile_breadth(leaf, _ctx(actor="")) is None
    out: list = []
    holds = asyncio.run(pol._eval_leaf(leaf, _ctx(actor=""), _CH([]), out))
    assert holds is False and out[0].skipped == "no actor on this call"


def test_evaluates_count_and_fraction_forms():
    ch = _CH([{"n": 120, "total": 1500}])
    out: list = []
    leaf = rules.BreadthWithin(op="breadth_within", within="1h", cmp="gt", value=100)
    assert asyncio.run(pol._eval_leaf(leaf, _ctx(), ch, out)) is True
    assert out[-1].value == {"distinct_keys": 120, "namespace_rows": 1500}
    leaf_f = rules.BreadthWithin(op="breadth_within", within="1h", cmp="gt", fraction=0.05)
    assert asyncio.run(pol._eval_leaf(leaf_f, _ctx(), ch, out)) is True
    assert out[-1].value["fraction"] == 0.08
    leaf_f2 = rules.BreadthWithin(op="breadth_within", within="1h", cmp="gt", fraction=0.10)
    assert asyncio.run(pol._eval_leaf(leaf_f2, _ctx(), ch, out)) is False
    # Empty namespace: fraction is 0, never holds on gt.
    ch0 = _CH([{"n": 0, "total": 0}])
    assert asyncio.run(pol._eval_leaf(leaf_f, _ctx(), ch0, out)) is False


def test_forbid_rule_denies_when_breadth_exceeds_the_budget():
    rule = rules.parse_rule(
        {
            "policy_id": "breadth-5pct",
            "effect": "forbid",
            "actions": ["memory.*"],
            "condition": {"op": "breadth_within", "within": "1h", "cmp": "gt", "fraction": 0.05},
        }
    )
    verdict = asyncio.run(pol.evaluate_rule(rule, _ctx(), _CH([{"n": 90, "total": 1000}])))
    assert verdict.denies is True
    verdict2 = asyncio.run(pol.evaluate_rule(rule, _ctx(), _CH([{"n": 40, "total": 1000}])))
    assert verdict2.denies is False


def test_gate_context_carries_the_verified_actor():
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.app.policy_gate import context_for
    from server.memory.api.ops_memory import OPERATIONS

    op = next(o for o in OPERATIONS if o.name == "memory.search")
    ident = {"org": "acme", "namespace": "shop", "agent": "webshop"}
    assert context_for(op, ident, {}).actor == ""
    set_current_auth(
        AuthInfo(
            subject="sa:shop-bot",
            org="acme",
            namespaces=("shop",),
            roles=(),
            is_service_account=True,
        )
    )
    try:
        assert context_for(op, ident, {}).actor == "sa:shop-bot"
    finally:
        set_current_auth(None)
