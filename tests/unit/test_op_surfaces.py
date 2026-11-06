"""Every op is reachable from HTTP, MCP, both SDKs and the dashboard, or says
why not in code: HTTP serves the whole registry; MCP reaches every op its
hidden roster does not keep for people; the two generated SDKs carry every op
(`test_sdk_parity.py`); the dashboard calls every op a person needs, and
`server/operations/dashboard.py` names the rest with a reason."""

from __future__ import annotations

import re
from pathlib import Path

from loom.ops import OPS as PY_OPS

from scripts.codegen.op_reach_census import dashboard_violations, derive_static_reach
from server.core.paths import REPO_ROOT
from server.operations.dashboard import (
    AGENT_STEP,
    CODE_QUESTION,
    DASHBOARD_PENDING,
    NOT_IN_DASHBOARD,
    TRACKER_QUESTION,
)
from server.operations.registry import ALL_OPERATIONS
from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.transport.http import build_router
from server.transport.mcp_op import hidden_refusal, reachable

# Ops a person needs whose dashboard control is not built yet. Lower it as
# controls land; it never goes up.
PENDING_CEILING = 78

NAMES = {op.name for op in ALL_OPERATIONS}


def test_http_serves_every_op():
    paths = {r.path for r in build_router().routes if "POST" in getattr(r, "methods", ())}
    assert {op.http_path for op in ALL_OPERATIONS} <= paths


def test_mcp_reaches_every_op_it_does_not_keep_for_people():
    """`loom_op` routes every op off the hidden roster (a read-only connection
    its read ops); a hidden op answers a refusal that says what to do instead."""
    for op in ALL_OPERATIONS:
        if op.name in _MCP_HIDDEN_OPS:
            assert hidden_refusal(op.name), op.name
        else:
            assert reachable(op, None), op.name
            assert reachable(op, "readonly") == op.read_only, op.name


def test_both_sdks_carry_every_op():
    ts = (REPO_ROOT / "sdk" / "typescript" / "src" / "generated" / "ops.ts").read_text()
    assert set(PY_OPS) == NAMES
    assert set(re.findall(r'\{ name: "([a-z_.]+)"', ts)) == NAMES


def test_every_op_is_on_the_dashboard_or_says_why_not():
    assert dashboard_violations(ALL_OPERATIONS) == []


def test_every_reason_is_a_sentence_of_its_own():
    for name, reason in NOT_IN_DASHBOARD.items():
        assert reason.strip() and reason[0].islower() and not reason.endswith("."), name


def test_an_op_kept_off_mcp_reaches_a_person_or_a_program():
    """An op MCP keeps for people and operators is on the dashboard, pending
    there, or called by a program; never an agent's step, which would leave it
    with no caller at all."""
    agent_only = {AGENT_STEP, CODE_QUESTION, TRACKER_QUESTION}
    for name in _MCP_HIDDEN_OPS & NAMES:
        assert NOT_IN_DASHBOARD.get(name) not in agent_only, name


def test_the_pending_set_only_shrinks():
    assert len(DASHBOARD_PENDING) <= PENDING_CEILING, (
        f"DASHBOARD_PENDING grew to {len(DASHBOARD_PENDING)}: a new op a person needs "
        "ships with its dashboard control"
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
        'const LABEL = "memory.redact";\n'
    )
    (src / "test" / "stub.ts").write_text('post("/v1/memory.touch", {});\n')
    (src / "Page.test.tsx").write_text('post("/v1/memory.unlock", {});\n')
    derived = derive_static_reach(tmp_path)
    assert derived == {"memory.pin": ("dashboard",), "memory.unpin": ("dashboard",)}
