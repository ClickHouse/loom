"""History policy: rule grammar, compiler, evaluator, cache."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.identity.app import history_policy as P
from server.identity.domain import policy_rules as R

NOW = datetime(2026, 9, 11, 21, 0, tzinfo=UTC)


def ctx(operation: str = "memory.set", **kw: Any) -> R.CallContext:
    base = dict(
        org="acme",
        namespace="team",
        agent="bot",
        user_id="u1",
        session_id="s1",
        operation=operation,
        args={"memory_key": "prefs"},
        now=NOW,
    )
    base.update(kw)
    return R.CallContext(**base)


class FakeCH:
    """Answers each query from a queue of canned rows; records SQL + params."""

    def __init__(self, answers: list[list[dict[str, Any]]] | None = None, fail: bool = False):
        self.answers = list(answers or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        if self.fail:
            raise RuntimeError("clickhouse unreachable")
        return self.answers.pop(0) if self.answers else []


# ---------------------------------------------------------------- grammar --


def test_window_units_and_bounds():
    assert R.window_seconds("1h") == 3600
    assert R.window_seconds("30m") == 1800
    assert R.window_seconds("2d") == 172_800
    for bad in ("1w", "h", "0s", "-1h", "1.5h", ""):
        with pytest.raises(ValueError):
            R.window_seconds(bad)


def test_window_is_capped_by_setting(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "policy_max_window_seconds", 3600)
    R.parse_rule(
        {
            "policy_id": "ok",
            "effect": "forbid",
            "actions": ["memory.*"],
            "condition": {"op": "formerly", "within": "1h", "operation": "memory.get"},
        }
    )
    with pytest.raises(ValueError, match="exceeds policy_max_window_seconds"):
        R.parse_rule(
            {
                "policy_id": "too-long",
                "effect": "forbid",
                "actions": ["memory.*"],
                "condition": {"op": "formerly", "within": "2h", "operation": "memory.get"},
            }
        )


def test_rule_rejects_bad_shapes():
    bad = [
        # unknown condition op
        {"op": "eventually", "within": "1h", "operation": "x"},
        # count needs cmp + value
        {"op": "count_within", "within": "1h", "operation": "x"},
        # not_since needs an anchor
        {"op": "not_since", "within": "1h", "operation": "x"},
        # empty operation list
        {"op": "formerly", "within": "1h", "operation": []},
        # args_equal key is not a field name
        {"op": "formerly", "within": "1h", "operation": "x", "args_equal": {"a b": "1"}},
    ]
    for cond in bad:
        with pytest.raises((ValueError, TypeError)):
            R.parse_rule(
                {"policy_id": "r", "effect": "forbid", "actions": ["*"], "condition": cond}
            )
    with pytest.raises(ValueError):
        R.parse_rule(
            {
                "policy_id": "r",
                "effect": "forbid",
                "actions": ["Memory Set"],
                "condition": {"op": "formerly", "within": "1h", "operation": "x"},
            }
        )
    with pytest.raises(ValueError, match="at most 8"):
        R.parse_rule(
            {
                "policy_id": "r",
                "effect": "forbid",
                "actions": ["*"],
                "condition": {
                    "op": "all",
                    "of": [
                        {
                            "op": "any",
                            "of": [{"op": "formerly", "within": "1h", "operation": "x"}] * 5,
                        },
                        {
                            "op": "any",
                            "of": [{"op": "formerly", "within": "1h", "operation": "x"}] * 5,
                        },
                    ],
                },
            }
        )


def test_actions_match_exact_glob_and_star():
    rule = R.parse_rule(
        {
            "policy_id": "r",
            "effect": "forbid",
            "actions": ["memory.delete", "code.*"],
            "condition": {"op": "formerly", "within": "1h", "operation": "x"},
        }
    )
    assert rule.applies_to("memory.delete")
    assert rule.applies_to("code.search")
    assert not rule.applies_to("memory.set")
    star = R.parse_rule(
        {
            "policy_id": "r",
            "effect": "forbid",
            "actions": ["*"],
            "condition": {"op": "formerly", "within": "1h", "operation": "x"},
        }
    )
    assert star.applies_to("anything.at_all")


# --------------------------------------------------------------- compiler --


def test_formerly_compiles_to_one_pinned_point_query():
    leaf = R.Formerly(
        op="formerly",
        within="1h",
        operation=["memory.get"],
        success=True,
        args_equal={"memory_key": "$args.memory_key"},
    )
    c = R.compile_leaf(leaf, ctx())
    assert c is not None and c.kind == "formerly"
    sql = c.sql
    # the pin: the caller's key, injected by the compiler
    assert "org = {p_org:String} AND namespace = {p_ns:String} AND agent = {p_agent:String}" in sql
    assert c.params["p_org"] == "acme" and c.params["p_agent"] == "bot"
    # closed window measured back from the decision time
    assert "ts <= {p_now:DateTime64(3)}" in sql
    assert "ts >= {p_now:DateTime64(3)} - INTERVAL {p_secs:UInt32} SECOND" in sql
    assert c.params["p_secs"] == 3600 and c.params["p_now"] == NOW
    # worker rows never count as history
    assert "kind != 'worker'" in sql and "startsWith(operation, 'worker.')" in sql
    # success filter and the args correlation resolved from the current call
    assert "success = {p_success:Bool}" in sql and c.params["p_success"] is True
    assert "args[{p_ak0:String}] = {p_av0:String}" in sql
    assert c.params["p_ak0"] == "memory_key" and c.params["p_av0"] == "prefs"
    assert c.params["p_ops"] == ["memory.get"]
    assert "session_id" not in sql and "user_id" not in sql
    # no literal from the call is ever spliced into the SQL text
    assert "acme" not in sql and "prefs" not in sql


def test_session_and_user_keys_narrow_the_pin():
    leaf = R.Formerly(op="formerly", within="1h", operation="x", key="session")
    c = R.compile_leaf(leaf, ctx())
    assert c is not None and "session_id = {p_session:String}" in c.sql
    assert c.params["p_session"] == "s1"
    leaf_u = R.Formerly(op="formerly", within="1h", operation="x", key="user")
    c2 = R.compile_leaf(leaf_u, ctx())
    assert c2 is not None and "user_id = {p_user:String}" in c2.sql and c2.params["p_user"] == "u1"


def test_unresolved_args_reference_skips_the_leaf():
    leaf = R.Formerly(
        op="formerly", within="1h", operation="x", args_equal={"memory_key": "$args.memory_id"}
    )
    assert R.compile_leaf(leaf, ctx()) is None
    # a literal template is not a reference and always compiles
    leaf2 = R.Formerly(
        op="formerly", within="1h", operation="x", args_equal={"memory_type": "fact"}
    )
    c = R.compile_leaf(leaf2, ctx())
    assert c is not None and c.params["p_av0"] == "fact"


def test_not_since_compiles_to_two_maxima():
    leaf = R.NotSince(
        op="not_since", within="24h", operation=["memory.delete"], anchor=["memory.set"]
    )
    c = R.compile_leaf(leaf, ctx())
    assert c is not None and c.kind == "not_since"
    assert "maxOrNullIf(ts, operation IN {p_anchor:Array(String)}) AS anchor_ts" in c.sql
    assert "maxOrNullIf(ts, operation IN {p_left:Array(String)}) AS left_ts" in c.sql
    assert c.params["p_anchor"] == ["memory.set"] and c.params["p_left"] == ["memory.delete"]


# -------------------------------------------------------------- evaluator --


def _rule(
    effect: str, cond: dict[str, Any], actions: list[str] | None = None, pid: str = "r1"
) -> R.PolicyRule:
    return R.parse_rule(
        {"policy_id": pid, "effect": effect, "actions": actions or ["memory.*"], "condition": cond}
    )


@pytest.mark.asyncio
async def test_require_formerly_denies_without_witness_and_allows_with_one():
    rule = _rule("require", {"op": "formerly", "within": "1h", "operation": "memory.get"})
    d = await P.evaluate([rule], ctx(), FakeCH([[{"n": 0}]]))
    assert d.decision == "deny" and d.determining_rules == ["r1"] and d.errors == []
    assert d.verdicts[0].leaves[0].value == 0 and d.verdicts[0].holds is False
    d2 = await P.evaluate([rule], ctx(), FakeCH([[{"n": 3}]]))
    assert d2.decision == "allow" and d2.determining_rules == []
    assert d2.verdicts[0].leaves[0].value == 3


@pytest.mark.asyncio
async def test_forbid_count_within_is_a_durable_rate_limit():
    rule = _rule(
        "forbid",
        {
            "op": "count_within",
            "within": "1h",
            "operation": "memory.set",
            "cmp": "gte",
            "value": 200,
        },
    )
    assert (await P.evaluate([rule], ctx(), FakeCH([[{"n": 199}]]))).decision == "allow"
    assert (await P.evaluate([rule], ctx(), FakeCH([[{"n": 200}]]))).decision == "deny"


@pytest.mark.asyncio
async def test_not_since_open_session_idiom():
    rule = _rule(
        "require",
        {"op": "not_since", "within": "24h", "operation": "memory.delete", "anchor": "memory.set"},
    )
    t0 = NOW - timedelta(hours=2)
    # anchor present, nothing forbidden since → holds
    d = await P.evaluate([rule], ctx(), FakeCH([[{"anchor_ts": t0, "left_ts": None}]]))
    assert d.decision == "allow"
    # forbidden event after the anchor → does not hold
    d = await P.evaluate(
        [rule], ctx(), FakeCH([[{"anchor_ts": t0, "left_ts": t0 + timedelta(minutes=5)}]])
    )
    assert d.decision == "deny"
    # no anchor in the window → since has nothing to anchor on → does not hold
    d = await P.evaluate([rule], ctx(), FakeCH([[{"anchor_ts": None, "left_ts": None}]]))
    assert d.decision == "deny"


@pytest.mark.asyncio
async def test_all_any_not_combine_and_short_circuit():
    f = {"op": "formerly", "within": "1h", "operation": "a"}
    g = {"op": "formerly", "within": "1h", "operation": "b"}
    ch = FakeCH([[{"n": 0}]])  # `all` stops after the first false leaf
    d = await P.evaluate([_rule("require", {"op": "all", "of": [f, g]})], ctx(), ch)
    assert d.decision == "deny" and len(ch.calls) == 1
    ch = FakeCH([[{"n": 1}]])  # `any` stops after the first true leaf
    d = await P.evaluate([_rule("require", {"op": "any", "of": [f, g]})], ctx(), ch)
    assert d.decision == "allow" and len(ch.calls) == 1
    d = await P.evaluate([_rule("forbid", {"op": "not", "of": f})], ctx(), FakeCH([[{"n": 0}]]))
    assert d.decision == "deny"  # forbid(not formerly) denies when nothing happened


@pytest.mark.asyncio
async def test_rules_that_do_not_apply_run_no_query_and_disabled_rules_are_skipped():
    ch = FakeCH()
    rule = _rule("forbid", {"op": "formerly", "within": "1h", "operation": "a"}, actions=["code.*"])
    d = await P.evaluate([rule], ctx("memory.set"), ch)
    assert d.decision == "allow" and d.verdicts == [] and ch.calls == []
    off = _rule("forbid", {"op": "formerly", "within": "1h", "operation": "a"})
    off.enabled = False
    d = await P.evaluate([off], ctx(), ch)
    assert d.verdicts == [] and ch.calls == []


@pytest.mark.asyncio
async def test_leaf_error_is_reported_never_a_verdict():
    rule = _rule("require", {"op": "formerly", "within": "1h", "operation": "a"})
    d = await P.evaluate([rule], ctx(), FakeCH(fail=True))
    assert d.decision == "allow"  # the evaluator does not decide on errors
    assert d.determining_rules == []
    assert d.errors == ["formerly: RuntimeError"]  # the store's text stays in the log
    assert d.verdicts[0].holds is None and d.verdicts[0].denies is False


@pytest.mark.asyncio
async def test_deny_overrides_across_rules_and_detail_explains():
    allow_rule = _rule("require", {"op": "formerly", "within": "1h", "operation": "a"}, pid="ok")
    deny_rule = _rule(
        "forbid",
        {"op": "count_within", "within": "1h", "operation": "b", "cmp": "gt", "value": 0},
        pid="rate",
    )
    d = await P.evaluate([allow_rule, deny_rule], ctx(), FakeCH([[{"n": 1}], [{"n": 5}]]))
    assert d.decision == "deny" and d.determining_rules == ["rate"]
    detail = d.to_detail()
    assert [r["policy_id"] for r in detail["rules"]] == ["ok", "rate"]
    assert detail["rules"][1]["leaves"][0]["value"] == 5


@pytest.mark.asyncio
async def test_rules_cache_hits_within_ttl_and_invalidates(monkeypatch):
    from server.identity.store import policy as policy_repo
    from server.settings import settings

    calls = {"n": 0}

    async def fake_list(org: str, ns: str, *, include_disabled: bool = True, ch: Any = None):
        calls["n"] += 1
        return [
            {
                "policy_id": "r",
                "effect": "forbid",
                "actions": ["*"],
                "condition": '{"op":"formerly","within":"1h","operation":["x"]}',
                "description": "",
                "enabled": True,
            }
        ]

    monkeypatch.setattr(policy_repo, "list_rules", fake_list)
    monkeypatch.setattr(settings, "policy_cache_ttl_seconds", 60)
    P.invalidate()
    r1 = await P.rules_for("o", "n")
    r2 = await P.rules_for("o", "n")
    assert calls["n"] == 1 and r1 == r2 and r1[0].policy_id == "r"
    P.invalidate("o", "n")
    await P.rules_for("o", "n")
    assert calls["n"] == 2
    P.invalidate()


@pytest.mark.asyncio
async def test_bad_stored_rule_does_not_poison_the_rest(monkeypatch):
    from server.identity.store import policy as policy_repo

    async def fake_list(org: str, ns: str, *, include_disabled: bool = True, ch: Any = None):
        return [
            {
                "policy_id": "broken",
                "effect": "forbid",
                "actions": ["*"],
                "condition": "{not json",
                "description": "",
                "enabled": True,
            },
            {
                "policy_id": "fine",
                "effect": "forbid",
                "actions": ["*"],
                "condition": '{"op":"formerly","within":"1h","operation":["x"]}',
                "description": "",
                "enabled": True,
            },
        ]

    monkeypatch.setattr(policy_repo, "list_rules", fake_list)
    P.invalidate()
    rules = await P.rules_for("o", "n2")
    assert [r.policy_id for r in rules] == ["fine"]
    P.invalidate()


@pytest.mark.asyncio
async def test_a_stored_rule_over_a_lowered_window_cap_is_clamped_not_dropped(monkeypatch, caplog):
    """Lowering policy_max_window_seconds must not silently stop enforcing an
    enforce-mode forbid rule stored with a longer window: it is enforced at
    the cap with one warning. A NEW rule over the cap is still refused."""
    from server.identity.store import policy as policy_repo
    from server.settings import settings

    monkeypatch.setattr(settings, "policy_max_window_seconds", 3600)
    stored = {
        "policy_id": "wide",
        "effect": "forbid",
        "actions": ["memory.delete"],
        "condition": json.dumps(
            {
                "op": "all",
                "of": [
                    {
                        "op": "count_within",
                        "within": "2d",
                        "operation": "x",
                        "cmp": "gt",
                        "value": 1,
                    }
                ],
            }
        ),
        "description": "",
        "enabled": True,
        "mode": "enforce",
    }

    async def fake_list(org, ns, ch=None):
        return [stored]

    monkeypatch.setattr(policy_repo, "list_rules", fake_list)
    P.invalidate()
    caplog.set_level("WARNING")
    rules = await P.rules_for("o", "clamp")
    assert [r.policy_id for r in rules] == ["wide"]
    leaf = rules[0].condition.of[0]
    assert R.window_seconds(leaf.within) == 3600
    assert any("enforced at the cap" in r.message for r in caplog.records)
    with pytest.raises(ValueError):
        R.parse_rule({**stored, "condition": json.loads(stored["condition"])})
    P.invalidate()


async def test_a_decision_recorded_before_the_upgrade_shows_the_error_kind(monkeypatch) -> None:
    """An older build recorded `<where>: <Kind>: <text>` in a decision's errors, and a
    viewer reads them through `policy.decisions`: the place and the kind remain."""
    from server.identity.api.models_policy import PolicyDecisionsInput
    from server.identity.app import history_policy_ops as ops
    from server.identity.store import policy as policy_repo

    text = (
        "Code: 210. Connection refused (ch-internal-7:9440) SELECT count() FROM loom.agent_events"
    )

    async def _decisions(org, namespace, **_kw) -> list[dict[str, Any]]:
        return [
            {
                "ts": datetime.now(UTC),
                "agent": "a",
                "user_id": "-",
                "session_id": "-",
                "operation": "memory.set",
                "request_id": "r",
                "mode": "enforce",
                "decision": "deny",
                "enforced": 1,
                "determining_rules": [],
                "errors": [
                    f"policy gate: RuntimeError: {text}",
                    f"formerly: RuntimeError: {text}",
                    "count_within: OVERLOADED: the server is busy; retry",
                    "formerly: RuntimeError",
                ],
                "latency_ms": 3,
            }
        ]

    monkeypatch.setattr(policy_repo, "list_decisions", _decisions)
    out = await ops.handle_policy_decisions(PolicyDecisionsInput(org="o", namespace="n"))
    assert out.rows[0].errors == [
        "policy gate: RuntimeError",
        "formerly: RuntimeError",
        "count_within: OVERLOADED: the server is busy; retry",
        "formerly: RuntimeError",
    ]
