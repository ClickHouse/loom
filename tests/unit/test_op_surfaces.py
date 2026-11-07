"""Every op is reachable from HTTP, MCP, both SDKs and the dashboard, or says
why not in code: HTTP serves the whole registry (`test_op_transport_contract.py`);
MCP reaches every op its hidden roster does not keep for people; the two
generated SDKs carry every op (`test_sdk_parity.py`); the dashboard calls every
op a person needs, and `server/operations/dashboard.py` names the rest with a
reason. That last rule is the census's `--check` (`test_op_reach_census.py`);
here it is tested case by case."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from scripts.codegen import op_reach_census as census
from server.operations.base import Role
from server.operations.dashboard import (
    AGENT_STEP,
    CODE_QUESTION,
    DASHBOARD_PENDING,
    NOT_IN_DASHBOARD,
    TRACKER_QUESTION,
    _named_once,
)
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.transport import mcp_op, mcp_tools

# The reasons that say only an agent calls the op.
AGENT_ONLY = {AGENT_STEP, CODE_QUESTION, TRACKER_QUESTION}

# The most DASHBOARD_PENDING may hold: an op leaves it when its control lands,
# and nothing joins it.
PENDING_AT_MOST = frozenset(
    {
        "code.calibrate",
        "code.reindex",
        "collection.move",
        "conversation.delete",
        "conversation.update",
        "cost.agent_summary",
        "grants.share_agent",
        "grants.unshare_agent",
        "knowledge.dataset_approve",
        "knowledge.dataset_get",
        "knowledge.ingest_cancel",
        "knowledge.source.inspect",
        "memory.record.conflicts",
        "memory.record.history",
        "memory.record.query",
        "memory_schema.approve",
        "memory_schema.list",
        "memory_schema.register",
        "namespace.guide_history",
        "namespace.list_categories",
        "namespace.list_ontology",
        "namespace.mark_seen",
        "namespace.register_category",
        "namespace.register_ontology",
        "procedure.list",
        "profile_schema.clear",
        "profile_schema.get",
        "profile_schema.set",
        "run.grade",
        "session.brief_delete",
    }
)


def _loom_op(name: str, toolset: str | None = None) -> tuple[dict, dict | None]:
    req = {
        "id": 1,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": {"op": name}},
    }
    return mcp_op.route(req, toolset=toolset, auth=None)


def test_mcp_reaches_every_op_it_does_not_keep_for_people(monkeypatch: pytest.MonkeyPatch):
    """`loom_op` rewrites a call for every op off the hidden roster into that
    op's own tool call; a hidden op answers a refusal that says what to do
    instead; a read-only connection routes reads only."""
    monkeypatch.setattr(mcp_op, "emit_token_refusal", lambda *a, **k: None)
    for op in ALL_OPERATIONS:
        req, answer = _loom_op(op.name)
        if op.name in _MCP_HIDDEN_OPS:
            assert answer["result"]["structuredContent"]["code"] == "OP_NOT_ON_MCP", op.name
        else:
            assert answer is None, op.name
            assert mcp_tools.TOOLS_BY_NAME[req["params"]["name"]] is op, op.name
        _, readonly_answer = _loom_op(op.name, "readonly")
        assert (readonly_answer is None) == (op.read_only and op.name not in _MCP_HIDDEN_OPS), (
            op.name
        )


def _op(name: str, *reach: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, reach=frozenset(reach))


@pytest.mark.parametrize(
    ("named", "pending", "op", "expected"),
    [
        (
            set(),
            set(),
            _op("code.ask"),
            "code.ask: no dashboard call site; add its control, or name it in "
            "NOT_IN_DASHBOARD with the reason",
        ),
        (
            {"code.ask"},
            set(),
            _op("code.ask", "dashboard"),
            "code.ask: the dashboard calls it, so drop it from server/operations/dashboard.py",
        ),
        (
            set(),
            {"code.ask"},
            _op("code.ask", "dashboard"),
            "code.ask: the dashboard calls it, so drop it from server/operations/dashboard.py",
        ),
        (
            {"code.ask"},
            {"code.ask"},
            _op("code.ask"),
            "code.ask: in both NOT_IN_DASHBOARD and DASHBOARD_PENDING",
        ),
        (
            {"x.gone"},
            set(),
            _op("code.ask", "dashboard"),
            "x.gone: in server/operations/dashboard.py but no such op",
        ),
    ],
)
def test_each_dashboard_violation_is_named(
    monkeypatch: pytest.MonkeyPatch, named: set, pending: set, op: SimpleNamespace, expected: str
):
    monkeypatch.setattr(census, "NOT_IN_DASHBOARD", dict.fromkeys(named, "a reason"))
    monkeypatch.setattr(census, "DASHBOARD_PENDING", frozenset(pending))
    assert census.dashboard_violations([op]) == [expected]


def test_an_agent_only_op_is_one_an_agent_token_may_call():
    for name, reason in NOT_IN_DASHBOARD.items():
        if reason in AGENT_ONLY:
            assert OPERATIONS_BY_NAME[name].required_role <= Role.WRITER, name


def test_an_op_kept_off_mcp_is_never_agent_only():
    """An op MCP keeps for people and operators is never given an agent-only
    reason: an agent cannot call it, so that reason would leave it with no
    caller."""
    for name in _MCP_HIDDEN_OPS:
        assert NOT_IN_DASHBOARD.get(name) not in AGENT_ONLY, name


def test_a_refusal_names_the_dashboard_only_for_an_op_the_dashboard_calls():
    """A hidden op with no alternative of its own sends the agent to the user in the
    dashboard when the dashboard calls it, and to an operator when it does not."""
    for name in _MCP_HIDDEN_OPS:
        instead = mcp_op.hidden_refusal(name)["instead"]
        called = "dashboard" in OPERATIONS_BY_NAME[name].reach
        if "dashboard" in instead:
            assert called, name
        if name not in mcp_op._INSTEAD:
            assert ("dashboard" in instead) == called, name
            assert ("operator" in instead) == (not called), name


def test_an_op_a_reason_names_is_on_the_dashboard():
    """A reason that says the dashboard does it through another op names one it calls."""
    for name, reason in NOT_IN_DASHBOARD.items():
        for other in re.findall(r"[a-z_]+(?:\.[a-z_]+)+", reason):
            assert other in OPERATIONS_BY_NAME, (name, other)
            assert "dashboard" in OPERATIONS_BY_NAME[other].reach, (name, other)


def test_the_pending_set_only_shrinks():
    joined = sorted(DASHBOARD_PENDING - PENDING_AT_MOST)
    assert not joined, f"{joined}: a new op a person needs ships with its dashboard control"


def test_an_op_named_twice_is_refused():
    with pytest.raises(ValueError, match="code.ask is named twice"):
        _named_once({"code.ask": "a"}, {"code.ask": "b"})


def test_an_op_a_refusal_sends_the_agent_to_is_on_mcp():
    """Every op and MCP tool a refusal names is one the agent can call."""
    for name, instead in mcp_op._INSTEAD.items():
        for tool in re.findall(r"\bloom_[a-z_]+\b", instead):
            assert tool in mcp_tools.TOOLS_BY_NAME or tool == mcp_op.LOOM_OP_TOOL_NAME, (name, tool)
        for other in re.findall(r"[a-z_]+(?:\.[a-z_]+)+", instead):
            assert other in OPERATIONS_BY_NAME and other not in _MCP_HIDDEN_OPS, (name, other)
