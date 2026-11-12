"""An op kept off MCP says why and what to call instead."""

from __future__ import annotations

from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.transport import mcp_op


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
    # a hidden write on readonly names no write: the connection cannot make one
    assert mcp_op.hidden_refusal("memory.bulk_set", "readonly")["instead"].startswith(
        "this connection is read-only"
    )


def test_a_readonly_deployment_default_routes_reads_only(monkeypatch) -> None:
    """With no ?toolset=, the deployment's readonly default governs loom_op as it
    governs the listing: a write is refused and the text says reads only."""
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_toolset", "readonly")
    assert not mcp_op.routes_writes(None)
    assert "read ops only" in mcp_op.loom_op_description(set(), None)
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


def test_a_readonly_refusal_keeps_every_reason_that_names_no_write() -> None:
    """Only an alternative this connection cannot reach becomes the read-only text."""
    instead = {
        name: mcp_op.hidden_refusal(name, "readonly")["instead"]
        for name in ("memory.unlock", "code.coverage.watch", "ui.vitals", "memory.bulk_set")
    }
    assert "only the user can unlock" in instead["memory.unlock"]
    assert instead["code.coverage.watch"].endswith("call code.index_status (via loom_op)")
    assert instead["ui.vitals"].endswith("nothing to call")
    assert instead["memory.bulk_set"].startswith("this connection is read-only")


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
