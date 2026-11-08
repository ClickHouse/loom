"""MCP session-layer surface: tool annotations, initialize instructions,
server-assigned `Mcp-Session-Id`, and description integrity.

Covers the registry-derived ToolAnnotations (no per-tool hand lists — new
ops must inherit correct hints), the primer delivered once via the
`initialize` result's `instructions` field (replacing the per-tool
"Default Loom workspace" preamble), the server-issued session id that the
session→agent provenance cache is keyed on, the loom_search outputSchema
matching what structuredContent actually returns, and a ghost-op detector
over the curated descriptions.

Hermetic: CH-touching helpers are monkeypatched; the FastAPI endpoint is
exercised in-process via TestClient.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

import server.transport.mcp as mcp
import server.transport.mcp_call as mcp_call
import server.transport.mcp_outcome as mcp_outcome
from server.operations import toolsets
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import mcp_config, mcp_primer, mcp_tools
from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

# ---- Tool annotations (derived from registry metadata) ----


def test_annotations_read_only_for_viewer_idempotent_ops() -> None:
    for name in (
        "memory.get",
        "memory.search",
        "memory.list",
        "memory.timeline",
        "memory.search_trace",
        "facts.traverse",
    ):
        ann = mcp_tools._tool_annotations(OPERATIONS_BY_NAME[name])
        assert ann["readOnlyHint"] is True, name
        assert ann["destructiveHint"] is False, name
        assert ann["idempotentHint"] is True, name


def test_annotations_destructive_for_delete_and_erasure_ops() -> None:
    for name in (
        "memory.delete",
        "knowledge.delete",
        "erasure.request",
        "working_memory.delete",
        "memory.redact",
    ):
        ann = mcp_tools._tool_annotations(OPERATIONS_BY_NAME[name])
        assert ann["readOnlyHint"] is False, name
        assert ann["destructiveHint"] is True, name


def test_annotations_erasure_reads_are_not_destructive() -> None:
    # Read-shaped verbs in the erasure group report on erasure; they
    # must not carry the destructive hint.
    assert (
        mcp_tools._tool_annotations(OPERATIONS_BY_NAME["erasure.export"])["destructiveHint"]
        is False
    )
    assert (
        mcp_tools._tool_annotations(OPERATIONS_BY_NAME["erasure.status"])["destructiveHint"]
        is False
    )


def test_annotations_plain_writes_are_neither_read_only_nor_destructive() -> None:
    ann = mcp_tools._tool_annotations(OPERATIONS_BY_NAME["memory.set"])
    assert ann == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}


def test_annotations_idempotent_hint_tracks_registry() -> None:
    assert (
        mcp_tools._tool_annotations(OPERATIONS_BY_NAME["memory.touch"])["idempotentHint"] is False
    )
    assert mcp_tools._tool_annotations(OPERATIONS_BY_NAME["memory.get"])["idempotentHint"] is True


async def test_tools_list_carries_annotations_everywhere() -> None:
    descriptors = await mcp._tools_descriptor()
    for d in descriptors:
        ann = d.get("annotations")
        assert isinstance(ann, dict), d["name"]
        assert set(ann) >= {"readOnlyHint", "destructiveHint", "idempotentHint"}, d["name"]
    by_name = {d["name"]: d for d in descriptors}
    # Synthetic tools are pure reads.
    assert by_name["loom_search"]["annotations"]["readOnlyHint"] is True
    assert by_name["loom_activate"]["annotations"]["readOnlyHint"] is True


# ---- Core toolset: the Refuse verb is in-band ----


def test_blacklist_add_is_core_and_not_hidden() -> None:
    assert "blacklist.add" in toolsets._MCP_CORE_OPS
    assert "blacklist.add" not in toolsets._MCP_HIDDEN_OPS
    # Management/list surfaces stay off the chat surface.
    assert "blacklist.list" in toolsets._MCP_HIDDEN_OPS
    assert "blacklist.remove" in toolsets._MCP_HIDDEN_OPS


async def test_blacklist_add_visible_in_default_tools_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_toolset", "core", raising=False)
    names = {d["name"] for d in await mcp._tools_descriptor()}
    assert "loom_blacklist_add" in names


# ---- Workspace preamble moved to initialize.instructions ----


async def test_descriptions_do_not_repeat_workspace_preamble() -> None:
    descriptors = await mcp._tools_descriptor()
    for d in descriptors:
        assert "Default Loom workspace" not in d["description"], d["name"]


def test_initialize_instructions_gate_profile_on_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def _rules(toolset: Any, defaults: Any) -> str:
        return "PRIMER"

    async def _profile(auth: Any, defaults: Any) -> str:
        seen.append(auth)
        return "\n\nPROFILE"

    async def _ctx(auth: Any, defaults: Any = None) -> str:
        return "\n\nWORKSPACE-CTX"

    async def _no_defaults(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(mcp_primer, "_loom_rules", _rules)
    monkeypatch.setattr(mcp_primer, "_profile_section", _profile)
    monkeypatch.setattr(mcp_primer, "_caller_default_context", _ctx)
    monkeypatch.setattr(mcp_primer, "_caller_defaults", _no_defaults)

    # Anonymous initialize (discovery_only/open modes) must not inline profile content.
    assert asyncio.run(mcp._initialize_instructions(None)) == "PRIMER\n\nWORKSPACE-CTX"
    assert seen == []

    token = object()
    out = asyncio.run(mcp._initialize_instructions(token))
    assert out == "PRIMER\n\nWORKSPACE-CTX\n\nPROFILE"
    assert seen == [token]


def test_initialize_result_carries_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instr(auth: Any, toolset: str | None = None) -> str:
        return "SESSION-START-PRIMER"

    monkeypatch.setattr(mcp, "_initialize_instructions", _instr)
    resp = asyncio.run(
        mcp._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            auth=None,
        )
    )
    result = resp["result"]
    assert result["instructions"] == "SESSION-START-PRIMER"
    # The handshake contract is otherwise unchanged.
    assert result["protocolVersion"] == mcp_config.MCP_PROTOCOL_VERSION
    assert "tools" in result["capabilities"]


def test_initialize_survives_instructions_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(auth: Any, toolset: str | None = None) -> str:
        raise RuntimeError("CH down")

    monkeypatch.setattr(mcp, "_initialize_instructions", _boom)
    resp = asyncio.run(
        mcp._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            auth=None,
        )
    )
    result = resp["result"]
    assert "instructions" not in result
    assert result["serverInfo"]["name"] == "loom"


def test_initialize_with_profile_leaves_a_ledger_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """An authenticated handshake inlines the profile, so it
    records one `mcp.initialize` agent_events row for the caller's default
    workspace; an anonymous one (no profile) records nothing."""

    async def _instr(auth: Any, toolset: str | None = None) -> str:
        return "PRIMER-WITH-PROFILE"

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": client_name or "x"}

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp, "_initialize_instructions", _instr)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    monkeypatch.setattr(mcp_primer, "_caller_defaults", _defaults)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda **kw: emitted.append(kw))
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda **kw: emitted.append(kw))
    monkeypatch.setattr(mcp_primer, "_emit_mcp_event", lambda **kw: emitted.append(kw))

    asyncio.run(
        mcp._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            auth=object(),
            client_name="cursor",
        )
    )
    assert [e["op_name"] for e in emitted] == [mcp_primer.INITIALIZE_OP]
    assert emitted[0]["raw_args"] == {"org": "o", "namespace": "ns", "agent": "cursor"}
    assert emitted[0]["success"] is True

    emitted.clear()
    asyncio.run(
        mcp._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            auth=None,
        )
    )
    assert emitted == []


def test_initialize_survives_ledger_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instr(auth: Any, toolset: str | None = None) -> str:
        return "PRIMER"

    async def _boom(auth: Any, client_name: str | None = None) -> dict[str, str]:
        raise RuntimeError("ledger down")

    monkeypatch.setattr(mcp, "_initialize_instructions", _instr)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _boom)
    monkeypatch.setattr(mcp_primer, "_caller_defaults", _boom)
    resp = asyncio.run(
        mcp._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            auth=object(),
        )
    )
    assert resp["result"]["instructions"] == "PRIMER"


# ---- Server-assigned Mcp-Session-Id ----


@pytest.fixture()
def _mcp_client(monkeypatch: pytest.MonkeyPatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.settings import settings

    # Open auth mode so the handshake needs no bearer; instructions are
    # stubbed so no CH/profile render happens.
    monkeypatch.setattr(settings, "identity_provider", "single-user", raising=False)

    async def _instr(auth: Any, toolset: str | None = None) -> str:
        return ""

    monkeypatch.setattr(mcp, "_initialize_instructions", _instr)
    app = FastAPI()
    mcp.attach(app)
    return TestClient(app)


def test_initialize_issues_session_id_and_caches_client(_mcp_client) -> None:
    resp = _mcp_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "Cursor"}},
        },
    )
    assert resp.status_code == 200
    sid = resp.headers.get("mcp-session-id")
    assert sid, "initialize must issue a server-assigned Mcp-Session-Id"
    try:
        # The provenance cache is keyed on the ISSUED id, so later POSTs
        # echoing the header resolve to the right client.
        assert mcp._MCP_AGENT_BY_SESSION.get(sid) == "cursor"
        # The id itself names the client, for the workers
        # whose cache never saw this initialize.
        assert sid.endswith(".cursor")
    finally:
        mcp._MCP_AGENT_BY_SESSION.pop(sid, None)


def test_session_id_echoed_on_subsequent_posts(_mcp_client) -> None:
    resp = _mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        headers={"Mcp-Session-Id": "established-session"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("mcp-session-id") == "established-session"


def test_initialize_keeps_established_session_id(_mcp_client) -> None:
    # Re-initialize on an existing session must not rotate the id —
    # the provenance cache entry would be orphaned.
    resp = _mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
        headers={"Mcp-Session-Id": "keep-me"},
    )
    assert resp.headers.get("mcp-session-id") == "keep-me"


# ---- loom_search outputSchema completeness ----


def test_loom_search_output_schema_declares_all_returned_sections() -> None:
    descriptors = mcp_tools._synthetic_tool_descriptors()
    loom_search = next(d for d in descriptors if d["name"] == "loom_search")
    props = set(loom_search["outputSchema"]["properties"])
    # structuredContent returns exactly these four sections; strict
    # client validators reject undeclared keys.
    assert props == {"memory", "knowledge", "profile", "conversations"}


# ---- Description integrity: no references to nonexistent ops ----


def test_descriptions_reference_only_registered_ops() -> None:
    groups = {name.split(".", 1)[0] for name in OPERATIONS_BY_NAME}
    pat = re.compile(r"\b[a-z_]+(?:\.[a-z_]+){1,2}\b")
    for tool, text in TOOL_DESCRIPTIONS.items():
        for ref in pat.findall(text):
            if ref in OPERATIONS_BY_NAME:
                continue
            # Trim a trailing segment the regex may have over-matched
            # (e.g. an op name followed by ".word" prose).
            head = ref.rsplit(".", 1)[0]
            if head in OPERATIONS_BY_NAME:
                continue
            assert ref.split(".", 1)[0] not in groups, (
                f"{tool!r} description references nonexistent op {ref!r}"
            )


def test_conversation_append_ghost_is_gone() -> None:
    text = TOOL_DESCRIPTIONS["conversation.append"]
    assert "namespace.set" not in text
    assert "conversation.set_logging" in text


@pytest.mark.asyncio
async def test_primer_never_asks_the_agent_to_log_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conversation.append accepts what a client sends; the primer never solicits it."""

    async def _defaults(*a: Any, **k: Any) -> dict[str, str]:
        return {"org": "clickhouse", "namespace": "alice", "agent": "claude-desktop"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    monkeypatch.setattr(mcp_primer, "_caller_defaults", _defaults)
    primer = mcp_primer._loom_rules(None, await mcp_primer._caller_defaults(object()))
    assert "loom_conversation_append" not in primer


@pytest.mark.parametrize("toolset", [*toolsets.MCP_TOOLSETS, "coding+tracker"])
def test_the_initialize_primer_names_only_tools_its_listing_carries(
    monkeypatch: pytest.MonkeyPatch, toolset: str
) -> None:
    """Every `loom_*` tool the handshake's instructions name is in the same
    connection's tools/list; an op the toolset does not list is named through
    `loom_op`, which every listing carries."""

    async def _defaults(*a: Any, **k: Any) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": "claude-code"}

    async def _ctx(auth: Any, defaults: Any = None) -> str:
        return ""

    monkeypatch.setattr(mcp_primer, "_caller_defaults", _defaults)
    monkeypatch.setattr(mcp_primer, "_caller_default_context", _ctx)
    monkeypatch.setattr(mcp_primer, "_mentor_line", _ctx)

    def _call(method: str) -> dict[str, Any]:
        out = asyncio.run(
            mcp._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}, toolset=toolset
            )
        )
        assert out is not None
        return out["result"]

    listed = {t["name"] for t in _call("tools/list")["tools"]}
    named = set(re.findall(r"`(loom_[a-z_]+)`", _call("initialize")["instructions"]))
    assert named and named <= listed, named - listed


def test_the_coding_primer_sends_documents_through_loom_op() -> None:
    coding = mcp_primer._loom_rules("coding", None)
    core = mcp_primer._loom_rules("core", None)
    assert "`loom_knowledge_add`" not in coding
    assert "`loom_op` (op `knowledge.add`)" in coding
    assert "`loom_knowledge_add`" in core and "(op `knowledge.add`)" not in core
    # The ops the coding listing carries keep their own tool.
    assert "`loom_memory_set`" in coding and "`loom_context_pack`" in coding


def test_the_guide_is_named_at_initialize_except_on_the_compact_coding_form() -> None:
    guide = "`loom_op` (op `mentor.guide`)"
    for toolset in ("core", "readonly", None):
        assert guide in asyncio.run(mcp._initialize_instructions(None, toolset)), toolset
    # A coding agent re-reads its instructions every turn and its plugin carries the
    # same guidance; the full primer (loom_activate, the prompt) still names the guide.
    assert guide not in asyncio.run(mcp._initialize_instructions(None, "coding"))
    full = mcp_primer._loom_rules("coding", None)
    assert guide in full


@pytest.mark.parametrize(("toolset", "writes"), [("readonly", False), ("coding", True)])
def test_the_primer_offers_saving_only_where_a_write_is_reachable(
    toolset: str, writes: bool
) -> None:
    """A connection whose listing carries no write tool and whose `loom_op`
    routes reads only gets no save rule; one that reaches memory.set does."""
    from server.transport import mcp_op

    primer = mcp_primer._loom_rules(toolset, None)
    listed_write = any(not op.read_only for op in mcp_tools.listed_ops(toolset))
    assert (listed_write or mcp_op.routes_writes(toolset)) is writes
    assert ("**3. Save.**" in primer) is writes
    assert ("memory writes are not available" in primer) is not writes
    assert ("loom_memory_set" in primer or "op `memory.set`" in primer) is writes
    assert ("write to Loom" in primer) is writes


def test_coding_toolset_initialize_instructions_are_the_compact_form() -> None:
    import asyncio

    from server.transport.mcp_primer import _initialize_instructions

    coding = asyncio.run(_initialize_instructions(None, "coding"))
    full = asyncio.run(_initialize_instructions(None, "core"))
    assert "loom_code_*" in coding and "loom_search" in coding
    assert "Loom-first mode" not in coding and "Loom-first mode" in full
    assert len(coding) * 3 < len(full)
