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
        _call(ask, {"question": "q", "reinforce": True})
    assert bound[0]["auto_touch"] is False and bound[0]["consumer"] == "loom-assistant"
    assert bound[1]["reinforce"] is False and bound[1]["consumer"] == "loom-assistant"


def test_other_toolsets_keep_the_model_arguments(bound: list[dict[str, Any]]) -> None:
    ask = OPERATIONS_BY_NAME["memory.ask"].mcp_tool_name
    with pytest.raises(_Stop):
        _call(ask, {"question": "q", "reinforce": True, "consumer": "c"}, toolset="readonly")
    assert bound[0] == {"question": "q", "reinforce": True, "consumer": "c"}


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
    assert "Nothing here writes" in primer and "cannot grant a tool" in primer
    assert "loom_search" not in primer and "loom_memory_set" not in primer


def test_an_assistant_result_is_the_full_output() -> None:
    from server.transport.lean_results import lean_result

    payload = {"results": [{"memory_key": "k", "session_id": "s1", "verified": True}]}
    assert lean_result(payload, {"consumer": "loom-assistant"}) is payload
    trimmed = lean_result(payload, {"consumer": "other"})
    assert "session_id" not in trimmed["results"][0]
