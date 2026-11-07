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
    assert "loom_memory_set" in body["instead"] and "did_you_mean" not in body


def test_unlock_says_only_the_owner_unlocks() -> None:
    body = _call("memory.unlock")["structuredContent"]
    assert body["code"] == "OP_NOT_ON_MCP" and "only the user" in body["instead"]


def test_every_hidden_op_gets_a_reason_and_an_unknown_op_keeps_the_old_refusal() -> None:
    from server.operations.registry import ALL_OPERATIONS

    names = {op.name for op in ALL_OPERATIONS}
    for name in sorted(_MCP_HIDDEN_OPS & names):
        body = mcp_op.hidden_refusal(name)
        assert body and body["instead"], name
    assert mcp_op.hidden_refusal("memory.nope") is None
    assert _call("memory.nope")["structuredContent"]["code"] == "UNKNOWN_OP"


def test_every_named_alternative_is_a_hidden_op() -> None:
    assert set(mcp_op._INSTEAD) <= _MCP_HIDDEN_OPS


def test_apply_says_it_never_creates_a_memory() -> None:
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    assert "never creates one" in TOOL_DESCRIPTIONS["memory.apply"]
