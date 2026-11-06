"""Every op is reachable from HTTP, MCP, both SDKs and the dashboard, or says
why not in code: HTTP serves the whole registry; MCP reaches every op its
hidden roster does not keep for people; the two generated SDKs carry every op
(`test_sdk_parity.py`); the dashboard calls every op a person needs, and
`server/operations/dashboard.py` names the rest with a reason."""

from __future__ import annotations

from pathlib import Path

from scripts.codegen.op_reach_census import dashboard_violations, derive_static_reach
from server.operations.dashboard import (
    AGENT_STEP,
    CODE_QUESTION,
    DASHBOARD_PENDING,
    NOT_IN_DASHBOARD,
    TRACKER_QUESTION,
)
from server.operations.registry import ALL_OPERATIONS
from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.transport import mcp_op
from server.transport.http import build_router

# The ops a person needs whose dashboard control is not built yet, as of the
# last control that landed. A PR that adds a control drops the op from both
# sets; nothing joins either.
PENDING_AT_MOST = frozenset(
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


def test_mcp_reaches_every_op_it_does_not_keep_for_people():
    """`loom_op` routes every op off the hidden roster, and its help catalog
    names them; a hidden op answers a refusal that says what to do instead."""
    routed = {row["op"] for row in mcp_op.catalog(limit=len(NAMES))["ops"]}
    assert routed == NAMES - _MCP_HIDDEN_OPS
    assert all(mcp_op.reachable(op, None) for op in ALL_OPERATIONS)
    for name in _MCP_HIDDEN_OPS & NAMES:
        assert mcp_op.hidden_refusal(name)["code"] == "OP_NOT_ON_MCP", name


def test_every_op_is_on_the_dashboard_or_says_why_not():
    assert dashboard_violations(ALL_OPERATIONS) == []


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


def test_the_pending_set_only_shrinks():
    grew = DASHBOARD_PENDING - PENDING_AT_MOST
    assert not grew, f"{sorted(grew)}: a new op a person needs ships with its dashboard control"
    assert DASHBOARD_PENDING == PENDING_AT_MOST, (
        "a control landed: drop the op from PENDING_AT_MOST too"
    )


def test_a_dashboard_call_site_is_a_quoted_path(tmp_path: Path):
    """A quoted `/v1/<op>` posts the op; a bare op name (an audit label, a
    comment) and the test helpers do not."""
    src = tmp_path / "frontend" / "src"
    (src / "test").mkdir(parents=True)
    (src / "api.ts").write_text(
        'post("/v1/memory.pin", body);\n'
        "post(`/v1/memory.unpin`, body);\n"
        "// POST /v1/memory.lock in a comment\n"
        "/** POST the bytes (`/v1/memory.touch?...`) */\n"
        'fetch("https://host/v1/x", {}); // `/v1/memory.reject`\n'
        'const LABEL = "memory.redact";\n'
    )
    (src / "test" / "stub.ts").write_text('post("/v1/memory.touch", {});\n')
    (src / "Page.test.tsx").write_text('post("/v1/memory.unlock", {});\n')
    derived = derive_static_reach(tmp_path)
    assert derived == {"memory.pin": ("dashboard",), "memory.unpin": ("dashboard",)}
