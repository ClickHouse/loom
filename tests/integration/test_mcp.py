"""MCP transport — JSON-RPC over HTTP at /mcp."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import pytest

from tests.integration._eventually import eventually

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def _writer_token() -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject="mcp-test",
        org="test",
        namespaces=["*"],
        roles=["writer"],
        ttl_seconds=300,
    )


async def _post(base_url: str, payload: dict | list, bearer: str | None) -> httpx.Response:
    """Raw JSON-RPC POST to /mcp; `bearer=None` sends no Authorization header."""
    headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as c:
        return await c.post("/mcp", json=payload, headers=headers)


async def _rpc(base_url: str, payload: dict | list) -> dict | list:
    """JSON-RPC POST to /mcp with a writer-scoped bearer (org=test, ns=*)."""
    r = await _post(base_url, payload, _writer_token())
    assert r.status_code == 200, f"/mcp returned {r.status_code}: {r.text}"
    return r.json()


@pytest.mark.asyncio
async def test_initialize_handshake(control_plane_url: str) -> None:
    resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
        },
    )
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"]["name"] == "loom"
    assert "tools" in result["capabilities"]


@pytest.mark.asyncio
async def test_tools_list_returns_all_operations(control_plane_url: str) -> None:
    resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        },
    )
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}

    # Derive the expected tool surface from the registry so it can't rot.
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import _MCP_CORE_OPS, _MCP_HIDDEN_OPS
    from server.settings import settings

    def _tool(op_name: str) -> str:
        return "loom_" + op_name.replace(".", "_")

    visible_ops = set(OPERATIONS_BY_NAME) - set(_MCP_HIDDEN_OPS)
    if settings.mcp_toolset == "core":
        visible_ops &= set(_MCP_CORE_OPS)

    _SYNTHETIC_TOOLS = {"loom_activate", "loom_search", "loom_op"}
    want = {_tool(n) for n in visible_ops} | _SYNTHETIC_TOOLS
    assert names == want, (
        f"MCP tool-set drift ({settings.mcp_toolset}) — "
        f"extra: {sorted(names - want)}; missing: {sorted(want - names)}"
    )
    assert {"loom_memory_get", "loom_memory_set", "loom_activate", "loom_search"} <= names
    assert "loom_blacklist_add" in names
    assert "loom_knowledge_search" not in names  # use loom_search
    # Pin is the standing-memory verb an agent controls; it is
    # in core, with the WHEN NOT pointing at memory.set.
    assert "loom_memory_pin" in names

    for tool in tools:
        assert "inputSchema" in tool
        # The default (standard) profile carries no outputSchema;
        # `?schema_profile=full` is the opt-in for hosts that validate results.
        assert "outputSchema" not in tool
        assert tool["inputSchema"].get("type") == "object"


@pytest.mark.asyncio
async def test_tools_call_memory_set_then_get(control_plane_url: str) -> None:
    """Round-trip a memory write + read via MCP."""
    ns = f"mcp-{uuid.uuid4().hex[:8]}"

    set_resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "loom_memory_set",
                "arguments": {
                    "org": "test",
                    "namespace": ns,
                    "agent": "mcp-agent",
                    "memory_key": "via-mcp",
                    "memory_type": "semantic",
                    "value": {"client": "mcp", "version": 7},
                },
            },
        },
    )
    assert set_resp["result"]["isError"] is False
    set_struct = set_resp["result"]["structuredContent"]
    # MCP coerces ints > 2^53 to strings to survive JS-side JSON.parse.
    set_version = int(set_struct["version"])
    assert set_version > 0

    # GET
    get_resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "loom_memory_get",
                "arguments": {
                    "org": "test",
                    "namespace": ns,
                    "agent": "mcp-agent",
                    "memory_key": "via-mcp",
                },
            },
        },
    )
    get_struct = get_resp["result"]["structuredContent"]
    assert get_struct["found"] is True
    assert get_struct["value"] == {"client": "mcp", "version": 7}
    assert int(get_struct["version"]) == set_version


@pytest.mark.asyncio
async def test_tools_call_unknown_tool(control_plane_url: str) -> None:
    resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {"name": "loom_does_not_exist", "arguments": {}},
        },
    )
    # Unknown tool surfaces as a tool-level error, not a JSON-RPC envelope error.
    assert "error" not in resp
    result = resp["result"]
    assert result["isError"] is True
    assert "not available" in result["structuredContent"]["error"]
    assert result["structuredContent"]["tool"] == "loom_does_not_exist"


@pytest.mark.asyncio
async def test_tools_call_invalid_arguments(control_plane_url: str) -> None:
    """Missing required fields → a tool result with isError whose text names
    the fields, so the model reads them (a -32602 frame's data never
    reaches it)."""
    resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            # org matches the bearer so authz passes and Pydantic surfaces the failure.
            "params": {"name": "loom_memory_set", "arguments": {"org": "test"}},
        },
    )
    result = resp["result"]
    assert result["isError"] is True
    assert "errors" in result["structuredContent"]
    text = result["content"][0]["text"]
    assert "loom_memory_set" in text or "memory.set" in text
    assert "required:" in text and "example:" in text


@pytest.mark.asyncio
async def test_method_not_found(control_plane_url: str) -> None:
    resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "does/not/exist",
        },
    )
    assert resp["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_initialized_notification_is_202_empty(control_plane_url: str) -> None:
    """`notifications/initialized` has no response per JSON-RPC; Streamable HTTP
    answers it with 202 Accepted and an empty body."""
    r = await _post(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",  # notifications have NO id
        },
        _writer_token(),
    )
    assert r.status_code == 202, f"/mcp returned {r.status_code}: {r.text}"
    assert r.content == b""


@pytest.mark.asyncio
async def test_garbage_bearer_is_401_with_challenge(control_plane_url: str) -> None:
    """An invalid bearer is an HTTP 401 with an RFC 6750
    challenge, and the JSON-RPC error frame echoes the request id (never null —
    MCP's `JSONRPCError.id` is string | number). Skips (via the fixture) when
    the deployment runs auth-off or `mcp_auth_mode=open`."""
    r = await _post(
        control_plane_url,
        {"jsonrpc": "2.0", "id": 8500, "method": "initialize", "params": {}},
        "garbage",
    )
    if r.status_code == 200 and "result" in r.json():
        pytest.skip("deployment accepts unauthenticated MCP (auth off or open mode)")
    assert r.status_code == 401, f"/mcp returned {r.status_code}: {r.text}"
    wa = r.headers.get("WWW-Authenticate", "")
    assert wa.startswith('Bearer realm="loom"'), wa
    assert 'error="invalid_token"' in wa, wa
    body = r.json()
    assert body["id"] == 8500
    assert body["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_batch_request(control_plane_url: str) -> None:
    """JSON-RPC batch: server returns an array of responses."""
    resp = await _rpc(
        control_plane_url,
        [
            {
                "jsonrpc": "2.0",
                "id": 50,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {"jsonrpc": "2.0", "id": 51, "method": "tools/list"},
        ],
    )
    assert isinstance(resp, list)
    assert len(resp) == 2
    assert resp[0]["id"] == 50 and "result" in resp[0]
    assert resp[1]["id"] == 51 and "tools" in resp[1]["result"]


@pytest.mark.asyncio
async def test_tools_call_blocks_lifecycle(control_plane_url: str) -> None:
    """Blocks self-edit via MCP: profile_set → append → replace."""
    ns = f"mcp-blk-{uuid.uuid4().hex[:8]}"
    agent = "mcp-block-agent"

    set_resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 60,
            "method": "tools/call",
            "params": {
                "name": "loom_profile_set",
                "arguments": {
                    "org": "test",
                    "namespace": ns,
                    "agent": agent,
                    "label": "persona",
                    "value": "MCP-tested persona",
                    "char_limit": 1024,
                },
            },
        },
    )
    assert set_resp["result"]["isError"] is False
    set_struct = set_resp["result"]["structuredContent"]
    assert set_struct["label"] == "persona"
    assert set_struct["char_limit"] == 1024
    # value_length = len(json.dumps("MCP-tested persona")) = 20
    assert set_struct["value_length"] == 20

    append_resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 61,
            "method": "tools/call",
            "params": {
                "name": "loom_profile_append",
                "arguments": {
                    "org": "test",
                    "namespace": ns,
                    "agent": agent,
                    "label": "persona",
                    "addition": "Loves columnar databases.",
                },
            },
        },
    )
    assert append_resp["result"]["isError"] is False
    append_struct = append_resp["result"]["structuredContent"]
    assert append_struct["char_limit"] == 1024
    assert append_struct["value_length"] == 47

    replace_resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 62,
            "method": "tools/call",
            "params": {
                "name": "loom_profile_replace",
                "arguments": {
                    "org": "test",
                    "namespace": ns,
                    "agent": agent,
                    "label": "persona",
                    "old": "columnar databases",
                    "new": "ClickHouse specifically",
                },
            },
        },
    )
    assert replace_resp["result"]["isError"] is False
    replace_struct = replace_resp["result"]["structuredContent"]
    assert replace_struct["char_limit"] == 1024
    # "ClickHouse specifically" (23) replaces "columnar databases" (18) → +5
    assert replace_struct["value_length"] == 52

    # Blocks are stored as memory rows, so loom_memory_get reads them back.
    get_resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 63,
            "method": "tools/call",
            "params": {
                "name": "loom_memory_get",
                "arguments": {
                    "org": "test",
                    "namespace": ns,
                    "agent": agent,
                    "memory_key": "persona",
                },
            },
        },
    )
    get_struct = get_resp["result"]["structuredContent"]
    assert get_struct["found"] is True
    assert get_struct["value"] == "MCP-tested persona\nLoves ClickHouse specifically."
    assert get_struct["char_limit"] == 1024


@pytest.mark.asyncio
async def test_tools_call_blocks_replace_refuses_missing_substring(
    control_plane_url: str,
) -> None:
    """blocks.replace on an absent substring → 422 SUBSTRING_NOT_FOUND, no silent no-op."""
    ns = f"mcp-blk-{uuid.uuid4().hex[:8]}"
    agent = "mcp-block-agent"

    await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 70,
            "method": "tools/call",
            "params": {
                "name": "loom_profile_set",
                "arguments": {
                    "org": "test",
                    "namespace": ns,
                    "agent": agent,
                    "label": "persona",
                    "value": "Stable persona text.",
                    "char_limit": 512,
                },
            },
        },
    )

    resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 71,
            "method": "tools/call",
            "params": {
                "name": "loom_profile_replace",
                "arguments": {
                    "org": "test",
                    "namespace": ns,
                    "agent": agent,
                    "label": "persona",
                    "old": "absolutely-not-there",
                    "new": "anything",
                },
            },
        },
    )
    # A handler 4xx is a refusal — an isError result whose
    # structuredContent carries the code and status, not a JSON-RPC error.
    assert "error" not in resp, f"expected a result, got: {resp}"
    result = resp["result"]
    assert result["isError"] is True
    body = result["structuredContent"]
    assert body["status"] == 422
    assert body["code"] == "SUBSTRING_NOT_FOUND" or "not found" in body["message"].lower()


@pytest.mark.asyncio
async def test_loom_activate_emits_agent_events(control_plane_url: str, ch_client) -> None:
    """A successful loom_activate (synthetic tool) emits an agent_events row."""
    resp = await _rpc(
        control_plane_url,
        {
            "jsonrpc": "2.0",
            "id": 80,
            "method": "tools/call",
            "params": {"name": "loom_activate", "arguments": {}},
        },
    )
    assert resp["result"]["isError"] is False
    primer = resp["result"]["structuredContent"]["primer"]
    assert "Loom-first mode active" in primer

    async def _activate_rows() -> list:
        return ch_client.query(
            """
            SELECT operation, success
            FROM loom.agent_events
            WHERE operation = 'loom_activate'
              AND ts > now() - INTERVAL 30 SECOND
              AND success = true
            ORDER BY ts DESC
            LIMIT 1
            """
        ).result_rows

    # The event emitter buffers; poll for the flush instead of guessing its interval.
    await eventually(_activate_rows, description="a loom_activate agent_events row")
