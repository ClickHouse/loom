"""Enforcement graduates per rule, outputs are correlatable,
and a policy can only quietly get stricter."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from server.identity.app import history_policy as P
from server.identity.app import policy_gate as G
from server.identity.domain import policy_rules as R
from server.identity.domain.policy_change import classify_change
from server.identity.store import policy as policy_repo
from server.observability.event_args import project_args, project_result
from server.operations.base import Operation, Role


def _rule(effect="require", mode="inherit", cond=None, actions=None, pid="r", enabled=True):
    return R.parse_rule(
        {
            "policy_id": pid,
            "effect": effect,
            "actions": actions or ["memory.set"],
            "condition": cond or {"op": "formerly", "within": "1h", "operation": "memory.get"},
            "enabled": enabled,
            "mode": mode,
        }
    )


# ------------------------------------------------------------ effective mode --


@pytest.mark.parametrize(
    "deployment,rule_mode,expected",
    [
        ("off", "enforce", "off"),  # off wins outright
        ("shadow", "inherit", "shadow"),
        ("shadow", "enforce", "enforce"),  # one rule graduates
        ("enforce", "inherit", "enforce"),
        ("enforce", "shadow", "shadow"),  # per-policy LOG_ONLY
    ],
)
def test_effective_mode(deployment, rule_mode, expected):
    assert _rule(mode=rule_mode).effective_mode(deployment) == expected


class FakeCH:
    def __init__(self, n=0, fail=False):
        self.n, self.fail = n, fail

    async def query_all(self, sql, params=None):
        if self.fail:
            raise RuntimeError("clickhouse unreachable")
        return [{"n": self.n}]


def ctx(op="memory.set"):
    return R.CallContext(
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        operation=op,
        args={"memory_key": "k"},
    )


@pytest.mark.asyncio
async def test_evaluate_separates_enforcing_from_shadow_denials():
    rules = [_rule(pid="quiet", mode="shadow"), _rule(pid="loud", mode="enforce")]
    d = await P.evaluate(rules, ctx(), FakeCH(n=0), deployment_mode="shadow")
    assert d.decision == "deny"
    assert d.determining_rules == ["quiet", "loud"]
    assert d.enforcing_rules == ["loud"]
    assert [v.mode for v in d.verdicts] == ["shadow", "enforce"]
    detail = d.to_detail()
    assert detail["enforcing_rules"] == ["loud"] and detail["rules"][0]["mode"] == "shadow"


@pytest.mark.asyncio
async def test_enforcing_errors_only_from_enforcing_rules():
    rules = [_rule(pid="quiet", mode="shadow")]
    d = await P.evaluate(rules, ctx(), FakeCH(fail=True), deployment_mode="enforce")
    assert d.errors and d.enforcing_errors == []
    rules = [_rule(pid="loud", mode="enforce")]
    d = await P.evaluate(rules, ctx(), FakeCH(fail=True), deployment_mode="shadow")
    assert d.enforcing_errors and d.enforcing_errors == d.errors


# ---------------------------------------------------------------------- gate --


class _In(BaseModel):
    org: str = "o"
    namespace: str = "n"


async def _noop(i: BaseModel) -> BaseModel:  # pragma: no cover
    return i


WRITE_OP = Operation(
    name="memory.set",
    input_model=_In,
    output_model=_In,
    handler=_noop,
    idempotent=False,
    budget_p99_ms=100,
    required_role=Role.WRITER,
)
IDENTITY = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"}


@pytest.fixture
def stored(monkeypatch):
    rows: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []

    async def fake_list(org, ns, *, include_disabled=True, ch=None):
        return rules

    async def fake_insert(row, *, ch=None):
        rows.append(row)

    monkeypatch.setattr(policy_repo, "list_rules", fake_list)
    monkeypatch.setattr(policy_repo, "insert_decision", fake_insert)
    P.invalidate()
    yield rules, rows
    P.invalidate()


def _row(pid, mode):
    return {
        "policy_id": pid,
        "effect": "require",
        "actions": ["memory.set"],
        "condition": '{"op":"formerly","within":"1h","operation":["memory.get"]}',
        "description": "",
        "enabled": True,
        "mode": mode,
    }


def _mode(monkeypatch, m):
    from server.settings import settings

    monkeypatch.setattr(settings, "policy_mode", m)


@pytest.mark.asyncio
async def test_rule_in_enforce_blocks_while_deployment_shadows(monkeypatch, stored):
    rules, rows = stored
    rules.append(_row("graduated", "enforce"))
    _mode(monkeypatch, "shadow")
    with pytest.raises(G.PolicyForbiddenError) as ei:
        await G.gate(WRITE_OP, IDENTITY, {}, ch=FakeCH(n=0))
    assert ei.value.to_detail()["enforcing_rules"] == ["graduated"]
    import asyncio

    await asyncio.sleep(0)
    assert rows and rows[0]["enforced"] is True and rows[0]["mode"] == "shadow"


@pytest.mark.asyncio
async def test_rule_in_shadow_never_blocks_while_deployment_enforces(monkeypatch, stored):
    rules, rows = stored
    rules.append(_row("log-only", "shadow"))
    _mode(monkeypatch, "enforce")
    d = await G.gate(WRITE_OP, IDENTITY, {}, ch=FakeCH(n=0))
    assert d is not None and d.decision == "deny" and d.enforcing_rules == []
    import asyncio

    await asyncio.sleep(0)
    assert rows and rows[0]["enforced"] is False and rows[0]["determining_rules"] == ["log-only"]


@pytest.mark.asyncio
async def test_shadow_rule_error_does_not_fail_closed_under_enforce(monkeypatch, stored):
    rules, _ = stored
    rules.append(_row("log-only", "shadow"))
    _mode(monkeypatch, "enforce")
    d = await G.gate(WRITE_OP, IDENTITY, {}, ch=FakeCH(fail=True))
    assert d is not None and d.decision == "allow" and d.errors


@pytest.mark.asyncio
async def test_off_still_wins_over_an_enforce_rule(monkeypatch, stored):
    rules, rows = stored
    rules.append(_row("graduated", "enforce"))
    _mode(monkeypatch, "off")
    assert await G.gate(WRITE_OP, IDENTITY, {}, ch=FakeCH(n=0)) is None
    assert rows == []


# ------------------------------------------------------- output correlation --


def test_project_result_keeps_identifiers_with_prefix_and_no_text():
    class Out(BaseModel):
        memory_id: str = "m-1"
        version: int = 42
        content_sha256: str = "abc"
        value: str = "secret text"
        namespace: str = "n"
        results: list[dict] = [{"a": 1}]

    out = project_result(Out())
    assert out == {"out_content_sha256": "abc", "out_memory_id": "m-1", "out_version": "42"}
    assert project_result(None) == {} and project_result("x") == {}
    # request args and result args coexist in one map without clashing
    merged = project_args({"memory_key": "k", "value": "v"})
    merged.update(project_result(Out()))
    assert "memory_key" in merged and "out_memory_id" in merged and "value" in merged
    assert merged["value"].startswith("sha256:")


def test_args_equal_can_correlate_on_a_prior_output():
    leaf = R.Formerly(
        op="formerly",
        within="1h",
        operation="memory.get",
        args_equal={"out_memory_id": "$args.memory_id"},
    )
    c = R.compile_leaf(
        leaf,
        R.CallContext(
            org="o",
            namespace="n",
            agent="a",
            user_id="u",
            session_id="s",
            operation="memory.delete",
            args={"memory_id": "m-1"},
        ),
    )
    assert c is not None and c.params["p_ak0"] == "out_memory_id" and c.params["p_av0"] == "m-1"


# ---------------------------------------------------------------- widening --


def _count(effect, value, cmp="gte", within="1h", actions=None, mode="inherit", enabled=True):
    return _rule(
        effect=effect,
        mode=mode,
        enabled=enabled,
        actions=actions,
        cond={
            "op": "count_within",
            "within": within,
            "operation": "memory.set",
            "cmp": cmp,
            "value": value,
        },
    )


def test_creating_and_tightening_is_quiet():
    assert classify_change(None, _count("forbid", 200)).widening is False
    v = classify_change(_count("forbid", 200), _count("forbid", 100))
    assert v.widening is False and "100" in v.reasons[0]
    v = classify_change(_rule(mode="shadow"), _rule(mode="enforce"))
    assert v.widening is False and v.reasons == ["mode shadow -> enforce"]


def test_widening_changes_are_named():
    v = classify_change(_count("forbid", 200), _count("forbid", 2000))
    assert v.widening and "200 -> 2000 (widens)" in v.reasons[0]
    v = classify_change(_rule(), _rule(enabled=False))
    assert v.widening and "disabled" in v.reasons
    v = classify_change(_rule(mode="enforce"), _rule(mode="shadow"))
    assert v.widening and v.reasons == ["mode enforce -> shadow"]
    v = classify_change(
        _rule(actions=["memory.set", "memory.delete"]), _rule(actions=["memory.set"])
    )
    assert v.widening and "memory.delete" in v.reasons[0]
    v = classify_change(_rule(), None)
    assert v.widening and v.reasons == ["rule deleted"]
    # a require gets easier with a longer window; a forbid gets harder to trip
    v = classify_change(
        _rule(cond={"op": "formerly", "within": "1h", "operation": "memory.get"}),
        _rule(cond={"op": "formerly", "within": "5h", "operation": "memory.get"}),
    )
    assert v.widening and "(widens)" in v.reasons[0]
    v = classify_change(_count("forbid", 200, within="1h"), _count("forbid", 200, within="5h"))
    assert v.widening is False
    # a require count gets easier when the threshold drops
    v = classify_change(_count("require", 5), _count("require", 2))
    assert v.widening


def test_unknown_shapes_are_conservatively_widening():
    v = classify_change(_rule(), _rule(effect="forbid"))
    assert v.widening and any("unknown" in r for r in v.reasons)
    v = classify_change(
        _rule(),
        _rule(
            cond={
                "op": "not_since",
                "within": "1h",
                "operation": "memory.delete",
                "anchor": "memory.set",
            }
        ),
    )
    assert v.widening and "restructured" in v.reasons[-1]
