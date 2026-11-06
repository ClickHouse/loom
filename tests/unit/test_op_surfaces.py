"""Every op is reachable from HTTP, MCP, both SDKs and the dashboard, or says
why not in code: HTTP serves the whole registry; MCP reaches every op its
hidden roster does not keep for people; the two generated SDKs carry every op
(`test_sdk_parity.py`); the dashboard calls every op a person needs, and
`server/operations/dashboard.py` names the rest with a reason. The dashboard
rule is also part of the census's `--check` (`test_op_reach_census.py`); here
it reads as the rule it is."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.codegen import op_reach_census as census
from server.operations.dashboard import (
    AGENT_STEP,
    CODE_QUESTION,
    DASHBOARD_PENDING,
    NOT_IN_DASHBOARD,
    TRACKER_QUESTION,
)
from server.operations.registry import ALL_OPERATIONS
from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.transport import mcp_op, mcp_tools
from server.transport.http import build_router

# DASHBOARD_PENDING as of the last change to it, so a change shows in this
# file too: a control that lands drops its op from both, and an op that joins,
# or moves to NOT_IN_DASHBOARD instead of getting a control, is a reviewer's
# question.
PENDING_PINNED = frozenset(
    {
        "blacklist.sweep",
        "calibration.get",
        "code.calibrate",
        "code.reindex",
        "collection.move",
        "conflicts.propose_revision",
        "conversation.delete",
        "conversation.update",
        "cost.agent_summary",
        "erasure.export",
        "erasure.request",
        "erasure.status",
        "extraction_policy.clear",
        "extraction_policy.get",
        "extraction_policy.set",
        "facts.expire_by_adapter",
        "grants.share_agent",
        "grants.unshare_agent",
        "knowledge.dataset_approve",
        "knowledge.dataset_get",
        "knowledge.dedup_list",
        "knowledge.dedup_resolve",
        "knowledge.ingest_cancel",
        "knowledge.source.inspect",
        "memory.agents_list",
        "memory.backlinks",
        "memory.clear",
        "memory.consolidate",
        "memory.dedup_collapse",
        "memory.dedup_list",
        "memory.dedup_resolve",
        "memory.demote",
        "memory.evidence",
        "memory.exposure_summary",
        "memory.extraction_status",
        "memory.health",
        "memory.lock",
        "memory.owner_report",
        "memory.promote",
        "memory.proposal_decide",
        "memory.proposals",
        "memory.record.conflicts",
        "memory.record.history",
        "memory.record.query",
        "memory.redact",
        "memory.reindex",
        "memory.reject",
        "memory.resolve_orphan_duplicates",
        "memory.sweep_expired",
        "memory.touch",
        "memory.unlock",
        "memory_schema.approve",
        "memory_schema.list",
        "memory_schema.register",
        "namespace.guide_history",
        "namespace.list_categories",
        "namespace.list_ontology",
        "namespace.mark_seen",
        "namespace.register_category",
        "namespace.register_ontology",
        "namespace.restore",
        "namespace.rollback",
        "namespace.snapshot",
        "ops.projection_failures.list",
        "ops.projection_failures.resolve",
        "ops.projection_failures.retry",
        "org.model_calls",
        "org.set_model_calls",
        "procedure.list",
        "profile_schema.clear",
        "profile_schema.get",
        "profile_schema.set",
        "run.grade",
        "session.brief_delete",
        "webhook.deliveries",
        "webhook.list",
        "webhook.register",
        "webhook.remove",
    }
)

NAMES = {op.name for op in ALL_OPERATIONS}


def test_http_serves_every_op():
    paths = {r.path for r in build_router().routes if "POST" in getattr(r, "methods", ())}
    assert {op.http_path for op in ALL_OPERATIONS} <= paths


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


def test_every_op_is_on_the_dashboard_or_says_why_not():
    assert census.dashboard_violations(ALL_OPERATIONS) == []


def _op(name: str, *reach: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, reach=frozenset(reach))


@pytest.mark.parametrize(
    ("skipped", "pending", "op", "expected"),
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
    monkeypatch: pytest.MonkeyPatch, skipped: set, pending: set, op: SimpleNamespace, expected: str
):
    monkeypatch.setattr(census, "NOT_IN_DASHBOARD", dict.fromkeys(skipped, "a reason"))
    monkeypatch.setattr(census, "DASHBOARD_PENDING", frozenset(pending))
    assert census.dashboard_violations([op]) == [expected]


def test_every_reason_is_a_lowercase_clause():
    for name, reason in NOT_IN_DASHBOARD.items():
        assert reason.strip() and reason[0].islower() and not reason.endswith("."), name


def test_an_op_kept_off_mcp_is_never_an_agent_step():
    """An op MCP keeps for people and operators is never classed as an agent's
    own step: an agent cannot call it, so that reason would leave it with no
    caller."""
    agent_only = {AGENT_STEP, CODE_QUESTION, TRACKER_QUESTION}
    for name in _MCP_HIDDEN_OPS & NAMES:
        assert NOT_IN_DASHBOARD.get(name) not in agent_only, name


def test_the_pending_set_is_pinned():
    assert DASHBOARD_PENDING == PENDING_PINNED, {
        "joined": sorted(DASHBOARD_PENDING - PENDING_PINNED),
        "left": sorted(PENDING_PINNED - DASHBOARD_PENDING),
    }


def test_a_dashboard_call_site_is_a_quoted_path(tmp_path: Path):
    """A quoted `/v1/<op>` posts the op; a bare op name (an audit label, a
    comment) and the test helpers do not."""
    src = tmp_path / "frontend" / "src"
    (src / "test").mkdir(parents=True)
    (src / "api.ts").write_text(
        'post("/v1/memory.pin", body);\n'
        "post(`/v1/memory.unpin`, body);\n"
        '// see "/v1/memory.lock"\n'
        "/** POST the bytes (`/v1/memory.touch?...`) */\n"
        'fetch("https://host/v1/x", {}); // `/v1/memory.reject`\n'
        'const hint = "**/generated/**, third_party/**";\n'
        'post("/v1/memory.redact", body);\n'
        '{/* the drawer posts\n   post("/v1/memory.unlock") */}\n'
        'const LABEL = "memory.redact";\n'
    )
    (src / "test" / "stub.ts").write_text('post("/v1/memory.touch", {});\n')
    (src / "Page.test.tsx").write_text('post("/v1/memory.unlock", {});\n')
    derived = census.derive_static_reach(tmp_path)
    assert derived == {
        "memory.pin": ("dashboard",),
        "memory.unpin": ("dashboard",),
        "memory.redact": ("dashboard",),
    }


def test_the_census_sources_add_no_reach():
    """The scan reads `scripts/`, the census's own home: its comments name no op."""
    assert "sdk_example" not in census.derive_static_reach(census.ROOT).get("memory.pin", ())
