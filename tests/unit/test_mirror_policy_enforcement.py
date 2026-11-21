"""Protected UNKNOWN fails closed without changing ordinary backend-error reads."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.auth import context as auth_context
from server.core import ch as ch_core
from server.identity.api.models_policy import PolicyCheckInput
from server.identity.app import history_policy, history_policy_ops, policy_gate
from server.identity.domain import policy_rules as rules
from server.observability import events
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings

_REASON = "protected history unavailable"
_IDENTITY = dict(org="acme", namespace="docs", agent="reader", user_id="person", session_id="run")
_MODES = [
    ("enforce", "inherit", True),
    ("enforce", "shadow", False),
    ("off", "enforce", False),
    ("shadow", "enforce", True),
]


def rule(mode="inherit", *, breadth=False, unresolved=False):
    leaf = dict(op="count_within", within="1h", operation=["knowledge.search"], cmp="lt", value=2)
    if breadth:
        leaf = dict(op="breadth_within", within="1h", key="actor", cmp="lt", value=2)
    if unresolved:
        leaf["args_equal"] = {"source_uri": "$args.source_uri"}
    return rules.PolicyRule(
        policy_id="guard",
        effect="require",
        actions=["memory.get", "memory.set"],
        condition=leaf,
        mode=mode,
    )


class History:
    def __init__(self, *, marked=False, rows=None, error=False):
        self.rows = [dict(protected_history=int(marked), n=1, total=2)] if rows is None else rows
        self.error = error
        self.calls = []

    async def query_all(self, sql, params):
        self.calls.append((sql, dict(params)))
        if self.error:
            raise ValueError("history backend unavailable")
        return self.rows


def call_context():
    return rules.CallContext(**_IDENTITY, operation="memory.get")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{}],
        [None],
        [dict(n=1)],
        [dict(protected_history=None, n=1)],
        [dict(protected_history="0", n=1)],
        [dict(protected_history=0.0, n=1)],
        [dict(protected_history=2, n=1)],
        [dict(protected_history=0, n=1), dict(protected_history=0, n=1)],
    ],
)
async def test_unverifiable_participation_is_unknown_before_reading_any_outcome(rows):
    ch = History(rows=rows)
    result = await history_policy.evaluate_rule(rule(), call_context(), ch)
    assert result.holds is None and result.denies is False
    assert result.errors == [_REASON]
    assert result.leaves[0].value is None and result.leaves[0].error == _REASON
    assert len(ch.calls) == 1


@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("negate", [False, True])
async def test_missing_argument_still_checks_coarse_participation_before_ordinary_false(
    marked, negate
):
    candidate = rule(unresolved=True)
    if negate:
        candidate = candidate.model_copy(
            update={"condition": rules.Not(op="not", of=candidate.condition)}
        )
    ch = History(marked=marked)
    result = await history_policy.evaluate_rule(candidate, call_context(), ch)
    assert len(ch.calls) == 1
    if marked:
        assert result.holds is None and result.denies is False
        assert result.errors == [_REASON] and result.leaves[0].value is None
    else:
        assert result.holds is negate and result.denies is (not negate)
        assert result.errors == []
        assert result.leaves[0].skipped == "args reference unresolved"
        assert result.leaves[0].value is None


@pytest.fixture
def gate_world(monkeypatch):
    records = []
    state = SimpleNamespace(candidate=rule(), mode="enforce", records=records)

    async def rules_for(*_args, **_kwargs):
        return [state.candidate]

    monkeypatch.setattr(policy_gate, "_mode", lambda: state.mode)
    monkeypatch.setattr(history_policy, "rules_for", rules_for)
    monkeypatch.setattr(policy_gate, "_record", lambda **kwargs: records.append(kwargs))
    return state


@pytest.mark.parametrize("operation", ["memory.get", "memory.set"])
@pytest.mark.parametrize("deployment,mode,blocks", _MODES)
async def test_protected_unknown_blocks_every_enforcing_operation_only(
    gate_world, operation, deployment, mode, blocks
):
    gate_world.candidate = rule(mode)
    gate_world.mode = deployment
    op = next(op for op in ALL_OPERATIONS if op.name == operation)
    ch = History(marked=True)
    if deployment == "off":
        assert await policy_gate.gate(op, _IDENTITY, None, ch=ch) is None
        assert ch.calls == [] and gate_world.records == []
        return
    if blocks:
        with pytest.raises(policy_gate.PolicyForbiddenError, match=_REASON):
            await policy_gate.gate(op, _IDENTITY, None, ch=ch)
    else:
        decision = await policy_gate.gate(op, _IDENTITY, None, ch=ch)
        assert decision.errors == [_REASON]
    assert len(ch.calls) == 1 and len(gate_world.records) == 1
    assert gate_world.records[0]["enforced"] is blocks
    assert gate_world.records[0]["decision"].errors == [_REASON]


@pytest.mark.parametrize("operation", ["memory.get", "memory.set"])
@pytest.mark.parametrize("deployment,mode,_blocks", _MODES)
async def test_unmarked_history_keeps_normal_gate_behavior(
    gate_world, operation, deployment, mode, _blocks
):
    gate_world.candidate = rule(mode)
    gate_world.mode = deployment
    op = next(op for op in ALL_OPERATIONS if op.name == operation)
    ch = History()
    if deployment == "off":
        assert await policy_gate.gate(op, _IDENTITY, None, ch=ch) is None
        assert ch.calls == [] and gate_world.records == []
        return
    decision = await policy_gate.gate(op, _IDENTITY, None, ch=ch)
    assert decision.errors == [] and decision.decision == "allow"
    assert len(ch.calls) == 1 and gate_world.records[0]["enforced"] is False


@pytest.mark.parametrize("operation,blocks", [("memory.get", False), ("memory.set", True)])
async def test_ordinary_backend_error_keeps_existing_read_and_write_posture(
    gate_world, operation, blocks
):
    op = next(op for op in ALL_OPERATIONS if op.name == operation)
    ch = History(error=True)
    if blocks:
        with pytest.raises(policy_gate.PolicyForbiddenError):
            await policy_gate.gate(op, _IDENTITY, None, ch=ch)
    else:
        decision = await policy_gate.gate(op, _IDENTITY, None, ch=ch)
        assert decision.errors and _REASON not in decision.errors
    assert len(ch.calls) == 1 and gate_world.records[0]["enforced"] is blocks


@pytest.mark.parametrize("operation", ["memory.get", "memory.set"])
@pytest.mark.parametrize("mode", ["enforce", "shadow"])
@pytest.mark.parametrize("marked", [False, True])
async def test_policy_check_pins_breadth_to_verified_subject_and_matches_protected_enforcement(
    monkeypatch, operation, mode, marked
):
    ch = History(marked=marked)
    flushed = []

    async def flush_now():
        flushed.append(True)

    monkeypatch.setattr(ch_core, "get_client", lambda: ch)
    monkeypatch.setattr(events, "get_emitter", lambda: SimpleNamespace(flush_now=flush_now))
    monkeypatch.setattr(auth_context, "current_auth", lambda: SimpleNamespace(subject="verified"))
    monkeypatch.setattr(settings, "policy_mode", "enforce")
    output = await history_policy_ops.handle_policy_check(
        PolicyCheckInput(
            **_IDENTITY, operation=operation, rules=[rule(mode, breadth=True).model_dump()]
        )
    )
    expected = marked and mode == "enforce"
    assert output.would_enforce is expected
    assert output.decision == ("deny" if expected else "allow")
    assert output.errors == ([_REASON] if marked else [])
    assert len(ch.calls) == 1 and ch.calls[0][1]["p_actor"] == "verified"
    assert flushed == [True]
