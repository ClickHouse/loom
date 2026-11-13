"""An op kept off MCP says why and what to call instead."""

from __future__ import annotations

import pytest

from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.transport import mcp_op
from tests.unit.loom_op_marks import assert_marked_once_outside_parentheses


def _call(op: str, arguments: dict | None = None) -> dict:
    req = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": {"op": op, "arguments": arguments or {}}},
    }
    _, resp = mcp_op.route(req, toolset=None, auth=None)
    assert resp is not None
    return resp["result"]


def test_bulk_set_names_memory_set_instead() -> None:
    res = _call("memory.bulk_set", {"items": []})
    body = res["structuredContent"]
    assert res["isError"] is True and body["code"] == "OP_NOT_ON_MCP"
    # named as the default listing names it
    assert "loom_memory_set once per fact" in body["instead"] and "did_you_mean" not in body


def test_a_refusal_names_ops_as_the_callers_listing_does() -> None:
    instead = {
        ts: mcp_op.hidden_refusal("memory.bulk_get", ts)["instead"]
        for ts in ("core", "coding", "readonly")
    }
    assert instead["core"] == (
        "call loom_memory_get per key, or memory.list (via loom_op) with a key prefix"
    )
    assert instead["coding"].startswith("call loom_memory_get per key")
    # readonly lists both reads
    assert (
        instead["readonly"] == "call loom_memory_get per key, or loom_memory_list with a key prefix"
    )


def test_a_readonly_deployment_default_routes_reads_only(monkeypatch) -> None:
    """With no ?toolset=, the deployment's readonly default governs loom_op as it
    governs the listing: a write is refused and the text says reads only."""
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_toolset", "readonly")
    assert not mcp_op.routes_writes(None)
    assert "Run a Loom read op" in mcp_op.loom_op_description(
        set(), writes=mcp_op.routes_writes(None)
    )
    body = _call("memory.set", {"memory_key": "k", "value": "v"})["structuredContent"]
    assert body["code"] == "UNKNOWN_OP" and "read-only" in body["error"]
    assert mcp_op.hidden_refusal("memory.bulk_set", None)["instead"].startswith(
        "this connection is read-only"
    )
    # a request that names another toolset keeps that toolset's routing
    assert mcp_op.routes_writes("core")
    req = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": {"op": "memory.set", "arguments": {}}},
    }
    rewritten, early = mcp_op.route(req, toolset="core", auth=None)
    assert early is None and rewritten["params"]["name"] == "loom_memory_set"


def test_unlock_says_only_the_owner_unlocks() -> None:
    body = _call("memory.unlock")["structuredContent"]
    assert body["code"] == "OP_NOT_ON_MCP" and "only the user" in body["instead"]


@pytest.mark.parametrize("name", sorted(mcp_op._INSTEAD))
def test_a_readonly_refusal_says_read_only_only_for_a_write_alternative(name: str) -> None:
    """On readonly, only bulk_set and delete_version (whose alternative is a write)
    get the read-only text; every other refusal keeps its reason."""
    instead = mcp_op.hidden_refusal(name, "readonly")["instead"]
    expected = name in {"memory.bulk_set", "memory.delete_version"}
    assert instead.startswith("this connection is read-only") == expected, (name, instead)


@pytest.mark.parametrize("name", sorted(mcp_op._INSTEAD))
def test_a_readonly_refusal_sends_the_model_only_where_it_can_go(name: str) -> None:
    """On readonly, every op a refusal names is listed there or routed by loom_op
    (scanned on its own, not with the renderer's pattern)."""
    import re

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.mcp_tools import listed_tool_names

    instead = mcp_op.hidden_refusal(name, "readonly")["instead"]
    listed = listed_tool_names("readonly")
    dotted = re.findall(r"\b[a-z_]+(?:\.[a-z_]+){1,2}\b", instead)
    tools = re.findall(r"\bloom_[a-z_]+\b", instead)
    ops = [OPERATIONS_BY_NAME[n] for n in dotted if n in OPERATIONS_BY_NAME]
    ops += [mcp_op.TOOLS_BY_NAME[t] for t in tools if t in mcp_op.TOOLS_BY_NAME]
    for op in ops:
        assert op.mcp_tool_name in listed or mcp_op.reachable(op, "readonly"), (name, op.name)


@pytest.mark.parametrize("toolset", ["core", "coding", "tracker", "readonly"])
def test_a_rendered_refusal_marks_each_op_once_outside_parentheses(toolset: str) -> None:
    for name in sorted(mcp_op._INSTEAD):
        text = mcp_op.hidden_refusal(name, toolset)["instead"]
        assert_marked_once_outside_parentheses(text, (toolset, name))


def test_a_readonly_refusal_keeps_its_reason() -> None:
    instead = {
        name: mcp_op.hidden_refusal(name, "readonly")["instead"]
        for name in (
            "memory.unlock",
            "code.coverage.watch",
            "ui.vitals",
            "memory.extraction_status",
        )
    }
    assert "only the user can unlock" in instead["memory.unlock"]
    assert instead["code.coverage.watch"].endswith("call code.index_status (via loom_op)")
    assert instead["ui.vitals"].endswith("nothing to call")
    assert instead["memory.extraction_status"].startswith(
        "call memory.extraction_status_mine (via loom_op) with the queue_id"
    )


def test_every_hidden_op_gets_a_reason_and_an_unknown_op_keeps_the_old_refusal() -> None:
    from server.operations.registry import ALL_OPERATIONS

    names = {op.name for op in ALL_OPERATIONS}
    for name in sorted(_MCP_HIDDEN_OPS & names):
        body = mcp_op.hidden_refusal(name, None)
        assert body and body["instead"], name
    assert mcp_op.hidden_refusal("memory.nope", None) is None
    assert _call("memory.nope")["structuredContent"]["code"] == "UNKNOWN_OP"


def test_every_named_alternative_is_a_hidden_op() -> None:
    assert set(mcp_op._INSTEAD) <= _MCP_HIDDEN_OPS


def test_apply_says_it_never_creates_a_memory() -> None:
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    assert "never creates one" in TOOL_DESCRIPTIONS["memory.apply"]


def test_a_direct_call_to_a_hidden_tool_names_ops_for_its_toolset() -> None:
    """tools/call of a hidden tool by name refuses with the same rendered text."""
    import asyncio

    from server.transport import mcp_call

    resp = asyncio.run(mcp_call._resolve_tool(9, "loom_memory_bulk_get", None, None, "coding"))
    body = resp["result"]["structuredContent"]
    assert body["code"] == "OP_NOT_ON_MCP" and body["tool"] == "loom_memory_bulk_get"
    assert body["instead"] == mcp_op.hidden_refusal("memory.bulk_get", "coding")["instead"]
    assert body["instead"].startswith("call loom_memory_get per key")
