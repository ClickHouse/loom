"""`?toolset=assistant` is a fixed set of reads, and nothing else is callable on it.

The listing carries exactly the roster's read-only ops (no `loom_op`, no `loom_search`); a
`tools/call` for any other tool is refused before its handler, whatever the token allows;
`loom_op` is not routed; and every call is ledgered as consumer `loom-assistant` with the
reads that record usage told not to, whatever the model sent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.transport.mcp as mcp
from server.operations.registry import OPERATIONS_BY_NAME
from server.operations.toolsets import (
    ASSISTANT_FORCED_ARGS,
    MCP_ASSISTANT_OPS,
    MCP_TOOLSETS,
    is_assistant,
    is_toolset,
)
from server.settings import settings
from server.transport import mcp_call, mcp_op, mcp_primer


@pytest.fixture(autouse=True)
def _no_ch_default_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(auth: Any, defaults: Any = None) -> str:
        return ""

    monkeypatch.setattr(mcp_primer, "_caller_default_context", _empty)


def _listed(toolset: str) -> set[str]:
    return {d["name"] for d in asyncio.run(mcp._tools_descriptor(toolset=toolset))}


def test_the_roster_is_reads_only_and_every_op_exists() -> None:
    assert "assistant" in MCP_TOOLSETS
    for name in MCP_ASSISTANT_OPS:
        assert OPERATIONS_BY_NAME[name].read_only, name
    # Every forced argument is a field of the op it is forced on.
    for name, forced in ASSISTANT_FORCED_ARGS.items():
        assert name in MCP_ASSISTANT_OPS
        assert set(forced) <= set(OPERATIONS_BY_NAME[name].input_model.model_fields)


def test_the_listing_is_exactly_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_code_tools", False)
    expected = {OPERATIONS_BY_NAME[n].mcp_tool_name for n in MCP_ASSISTANT_OPS}
    assert _listed("assistant") == expected
    assert "loom_op" not in expected and "loom_search" not in expected


def test_assistant_never_joins_a_union() -> None:
    assert is_assistant("assistant") and is_assistant("Assistant")
    assert not is_assistant("assistant+core")
    # A union naming it is no toolset at all: the deployment default lists instead.
    assert not is_toolset("assistant+core")


def _call(name: str, arguments: dict[str, Any], toolset: str = "assistant") -> dict[str, Any]:
    return asyncio.run(
        mcp_call._call_tool(1, {"name": name, "arguments": arguments}, None, None, "", toolset)
    )


class _Stop(Exception):
    pass


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The arguments each call reached identity binding with; the call stops there."""
    seen: list[dict[str, Any]] = []

    async def _bind(op: Any, arguments: dict[str, Any], auth: Any, client_name: Any) -> Any:
        seen.append(dict(arguments))
        raise _Stop

    monkeypatch.setattr(mcp_call, "_bind_caller_identity", _bind)
    return seen


@pytest.mark.parametrize("tool", ["loom_memory_set", "loom_memory_delete", "loom_op", "nope"])
def test_a_tool_outside_the_roster_is_refused_before_its_handler(
    bound: list[dict[str, Any]], tool: str
) -> None:
    out = _call(tool, {"key": "k", "value": "v"})
    assert out["result"]["isError"] is True
    assert "not available on the assistant toolset" in out["result"]["content"][0]["text"]
    assert out["result"]["structuredContent"]["code"] == mcp_op.UNKNOWN_OP
    assert bound == []


def test_a_read_outside_the_roster_is_refused_too(bound: list[dict[str, Any]]) -> None:
    op = OPERATIONS_BY_NAME["memory.search_trace_log"]
    assert op.read_only and op.name not in MCP_ASSISTANT_OPS
    assert _call(op.mcp_tool_name, {})["result"]["isError"] is True
    assert bound == []


def test_forced_arguments_and_the_consumer_override_the_model(
    bound: list[dict[str, Any]],
) -> None:
    search = OPERATIONS_BY_NAME["memory.search"].mcp_tool_name
    with pytest.raises(_Stop):
        _call(search, {"query": "q", "auto_touch": True, "consumer": "someone-else"})
    ask = OPERATIONS_BY_NAME["memory.ask"].mcp_tool_name
    with pytest.raises(_Stop):
        _call(ask, {"query": "q", "reinforce": True})
    assert bound[0]["auto_touch"] is False and bound[0]["explain"] is False
    assert bound[0]["consumer"] == "loom-assistant"
    assert bound[1]["reinforce"] is False and bound[1]["consumer"] == "loom-assistant"


def test_other_toolsets_keep_the_model_arguments(bound: list[dict[str, Any]]) -> None:
    ask = OPERATIONS_BY_NAME["memory.ask"].mcp_tool_name
    with pytest.raises(_Stop):
        _call(ask, {"query": "q", "reinforce": True, "consumer": "c"}, toolset="readonly")
    assert bound[0] == {"query": "q", "reinforce": True, "consumer": "c"}


def test_the_hidden_plane_searches_open_on_the_assistant_toolset_only(
    bound: list[dict[str, Any]],
) -> None:
    search = OPERATIONS_BY_NAME["memory.search"].mcp_tool_name
    refused = _call(search, {"query": "q"}, toolset="readonly")["result"]
    assert refused["isError"] and refused["structuredContent"]["code"] == "OP_NOT_ON_MCP"
    assert bound == []
    with pytest.raises(_Stop):
        _call(search, {"query": "q"})
    assert len(bound) == 1


def test_loom_op_is_not_routed_on_the_assistant_toolset() -> None:
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": {"op": "memory.set", "arguments": {}}},
    }
    assert mcp_op.route(req, toolset="assistant", auth=None) == (req, None)


def test_the_assistant_primer_is_read_only_and_names_listed_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primer = asyncio.run(
        mcp_primer._loom_primer_text(
            None, include_profile=False, toolset="assistant", defaults={"namespace": "ns"}
        )
    )
    import re

    assert "Nothing here writes" in primer and "cannot grant a tool" in primer
    monkeypatch.setattr(settings, "mcp_code_tools", False)
    named = set(re.findall(r"\bloom_[a-z_]+\b", primer))
    assert named and named <= _listed("assistant"), named - _listed("assistant")


def test_an_assistant_result_is_the_full_output() -> None:
    from server.transport.assistant_call import set_assistant_call
    from server.transport.lean_results import lean_result

    payload = {"results": [{"memory_key": "k", "session_id": "s1", "verified": True}]}
    set_assistant_call(True)
    try:
        assert lean_result(payload, {}) is payload
    finally:
        set_assistant_call(False)
    # The connection decides, never the self-reported consumer.
    trimmed = lean_result(payload, {"consumer": "loom-assistant"})
    assert "session_id" not in trimmed["results"][0]


def test_no_listed_description_names_a_tool_the_listing_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    monkeypatch.setattr(settings, "mcp_code_tools", False)
    descriptors = asyncio.run(mcp._tools_descriptor(toolset="assistant"))
    listed = {d["name"] for d in descriptors}
    for d in descriptors:
        named = set(re.findall(r"\bloom_[a-z_]+\b", d["description"]))
        assert named <= listed, (d["name"], named - listed)


@pytest.mark.parametrize(
    "defaults",
    [{"namespace": "", "readable": ["a", "b"]}, {"namespace": "", "readable": []}],
)
def test_the_assistant_primer_for_a_token_with_no_default_namespace(
    defaults: dict[str, Any],
) -> None:
    primer = asyncio.run(
        mcp_primer._loom_primer_text(
            None, include_profile=False, toolset="assistant", defaults=defaults
        )
    )
    assert "loom_search" not in primer
    assert "no default namespace" in primer
    assert all(f"`{ns}`" in primer for ns in defaults["readable"])


def test_the_catalogue_reports_the_assistant_roster() -> None:
    from server.operations.toolsets import op_toolsets

    for name in MCP_ASSISTANT_OPS:
        assert "assistant" in op_toolsets(name), name
    # A hidden op on the roster is listed there only.
    assert op_toolsets("memory.search") == ["assistant"]
    assert "assistant" not in op_toolsets("memory.set")


def test_the_listing_offers_no_argument_the_connection_overwrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_code_tools", False)
    by_name = {d["name"]: d for d in asyncio.run(mcp._tools_descriptor(toolset="assistant"))}
    for op_name, forced in ASSISTANT_FORCED_ARGS.items():
        props = by_name[OPERATIONS_BY_NAME[op_name].mcp_tool_name]["inputSchema"]["properties"]
        assert not set(forced) & set(props), op_name
    # Other listings keep them.
    core = {d["name"]: d for d in asyncio.run(mcp._tools_descriptor(toolset="atomic"))}
    assert "reinforce" in core["loom_memory_ask"]["inputSchema"]["properties"]


def test_an_unknown_tool_name_writes_no_ledger_row(
    bound: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[str] = []
    monkeypatch.setattr(mcp_call, "emit_token_refusal", lambda name, *a, **k: rows.append(name))
    _call("x" * 500, {})
    _call("loom_memory_set", {})
    assert rows == ["memory.set"]


def test_an_assistant_call_opens_no_session_run() -> None:
    from server.transport.assistant_call import set_assistant_call

    op = OPERATIONS_BY_NAME["memory.search"]
    defaults = {"org": "o", "namespace": "ns", "agent": "a"}
    set_assistant_call(True)
    try:
        assert mcp_call._stamp_session_run(op, {"query": "q"}, defaults, "sess-1") == (None, False)
    finally:
        set_assistant_call(False)
    # A self-reported inspector consumer on another connection keeps its run.
    caller, _ = mcp_call._stamp_session_run(op, {"consumer": "loom-ui"}, defaults, "sess-1")
    assert caller is not None


def test_an_assistant_call_through_the_call_path_opens_no_session_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer is stamped before the run phase, so the run phase sees it."""
    seen: list[tuple[Any, bool]] = []
    real = mcp_call._stamp_session_run

    async def _defaults(*a: Any, **k: Any) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": "a"}

    def _stamp(op: Any, arguments: dict[str, Any], defaults: Any, sid: str) -> Any:
        seen.append(real(op, arguments, defaults, sid))
        raise _Stop

    monkeypatch.setattr("server.transport.mcp_call._caller_defaults", _defaults)
    monkeypatch.setattr(mcp_call, "_stamp_session_run", _stamp)
    search = OPERATIONS_BY_NAME["memory.search"].mcp_tool_name
    with pytest.raises(_Stop):
        asyncio.run(
            mcp_call._call_tool(
                1, {"name": search, "arguments": {"query": "q"}}, None, None, "sess-1", "assistant"
            )
        )
    assert seen == [(None, False)]


@pytest.mark.parametrize("method", ["initialize", "prompts/get"])
def test_initialize_and_the_prompt_write_no_profile_read_row(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    emitted: list[Any] = []
    monkeypatch.setattr(mcp_primer, "_emit_mcp_event", lambda **k: emitted.append(k))

    async def _emit_init(*a: Any, **k: Any) -> None:
        emitted.append("initialize")

    monkeypatch.setattr(mcp, "_emit_initialize_event", _emit_init)

    async def _defaults(*a: Any, **k: Any) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": "a"}

    monkeypatch.setattr(mcp_primer, "_caller_defaults", _defaults)
    params = {"name": "loom"} if method == "prompts/get" else {}
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    out = asyncio.run(mcp._handle_request(req, auth=object(), toolset="assistant"))
    assert out is not None and "result" in out
    assert emitted == []
    # Elsewhere the prompt is a profile read and leaves its row.
    asyncio.run(mcp._handle_request(req, auth=object(), toolset="core"))
    assert emitted


def test_the_prompt_and_resource_describe_the_assistant_primer() -> None:
    assert (
        "read-only investigation" in mcp_primer._prompts_descriptor("assistant")[0]["description"]
    )
    assert "persona" in mcp_primer._prompts_descriptor("core")[0]["description"]
    assert "read-only" in mcp_primer._resources_descriptor("assistant")[0]["description"]


@pytest.mark.asyncio
async def test_namespace_summary_leaves_the_inspectors_out_of_its_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.identity.api.models_namespace_catalog import NamespaceSummaryInput
    from server.identity.app.namespace_summary import handle_namespace_summary
    from tests.unit.ch_fake import RecordingCH

    ch = RecordingCH(strict=False).install(monkeypatch, "server.identity.app.namespace_summary")
    await handle_namespace_summary(NamespaceSummaryInput(org="o", namespace="ns"))
    activity = ch.statements("FROM loom.agent_events")[0]
    assert "consumer NOT IN {inspectors:Array(String)}" in activity.sql
    assert activity.params["inspectors"] == ["loom-ui", "loom-assistant"]
