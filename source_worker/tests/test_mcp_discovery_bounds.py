from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from loom_source_worker import mcp_client
from loom_source_worker.mcp_client import (
    MCPDataPolicy,
    MCPLimits,
    MCPSourceClient,
    MCPSourceConfig,
    MCPSourceError,
    MCPToolMapping,
)

_ENDPOINT = "https://source.example/mcp"
_DOCUMENT = "mcp://fixture/document"
_PRIVATE_MARKER = "synthetic-private-malformed-response"
_MALFORMED = b'{"result":"' + _PRIVATE_MARKER.encode() + b'",'
_INPUT_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
_OUTPUT_SCHEMA = {"type": "object"}
_APPROVED_TOOL = {"name": "read", "inputSchema": _INPUT_SCHEMA, "outputSchema": _OUTPUT_SCHEMA}
_UNAPPROVED_TOOL = {"name": "other", "inputSchema": {"type": "object"}}


class _Body(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.content


class _Source(httpx.MockTransport):
    def __init__(self) -> None:
        super().__init__(self.respond)
        self.methods: list[str] = []
        self.cursors: list[str | None] = []
        self.pages: dict[str | None, dict[str, Any]] = {}
        self.malformed_method: str | None = None
        self.malformed_body = _MALFORMED
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        message = json.loads(request.content)
        method = message["method"]
        self.methods.append(method)
        if method == self.malformed_method:
            body = self.malformed_body
        elif method == "notifications/initialized":
            return httpx.Response(202, stream=_Body(b""))
        else:
            if method == "initialize":
                result = {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"resources": {}, "tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
            elif method == "tools/list":
                cursor = message.get("params", {}).get("cursor")
                self.cursors.append(cursor)
                result = self.pages[cursor]
            else:
                pytest.fail(f"unexpected fixture method: {method}")
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            body = json.dumps(reply).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Body(body))


@pytest.fixture(autouse=True)
def _pin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str) -> ipaddress.IPv6Address:
        assert host == "source.example"
        return ipaddress.IPv6Address("2600::10")

    monkeypatch.setattr(mcp_client, "resolve_public_address", resolve)


async def _authorized() -> None:
    return None


def _mapped_client(source: _Source, policy: MCPDataPolicy) -> MCPSourceClient:
    config = MCPSourceConfig(
        _ENDPOINT,
        approved_tools=("read",),
        read=MCPToolMapping("read", _INPUT_SCHEMA, _OUTPUT_SCHEMA, "id"),
        data_policy=policy,
    )
    return MCPSourceClient(config, None, validate_access=_authorized, _transport=source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "fields", "policy"),
    [
        ("approved", {"description": "é" * 17}, MCPDataPolicy(max_string_bytes=32)),
        ("approved", {"_meta": {"labels": [1, 2, 3]}}, MCPDataPolicy(max_items=2)),
        ("approved", {"extension": list(range(64))}, MCPDataPolicy(max_nodes=48)),
        ("page", {"_meta": {"extension": "é" * 17}}, MCPDataPolicy(max_string_bytes=32)),
        ("page", {"extension": list(range(64))}, MCPDataPolicy(max_nodes=48)),
        (
            "unapproved",
            {"outputSchema": {"type": "object", "examples": [[[[[True]]]]]}},
            MCPDataPolicy(max_depth=7),
        ),
    ],
)
async def test_discovery_bounds_complete_page_before_filtering(
    location: str, fields: dict[str, Any], policy: MCPDataPolicy
) -> None:
    source = _Source()
    page: dict[str, Any] = {"tools": [_APPROVED_TOOL]}
    if location == "page":
        page.update(fields)
    elif location == "approved":
        page["tools"] = [{**_APPROVED_TOOL, **fields}]
    else:
        page["tools"].append({**_UNAPPROVED_TOOL, **fields})
    source.pages[None] = page

    async with _mapped_client(source, policy) as client:
        with pytest.raises(MCPSourceError, match="^MCP source limit$"):
            await client.discover()

    assert source.cursors == [None]
    assert source.closed


@pytest.mark.asyncio
async def test_bounded_discovery_pages_without_the_sdk_caching_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse_helper(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the SDK listing helper caches provider schemas before policy validation")

    monkeypatch.setattr(mcp_client.ClientSession, "list_tools", refuse_helper)
    source = _Source()
    source.pages = {
        None: {
            "tools": [{**_UNAPPROVED_TOOL, "outputSchema": {"not": {"type": "number"}}}],
            "nextCursor": "next",
            "_meta": {"note": "bounded"},
        },
        "next": {"tools": [{**_APPROVED_TOOL, "description": "bounded"}]},
    }
    async with _mapped_client(source, MCPDataPolicy(max_depth=7)) as client:
        assert (await client.discover()).tools == ("read",)

    assert source.cursors == [None, "next"]
    assert source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["initialize", "resources/read"])
@pytest.mark.parametrize("revoked_on_close", [False, True])
async def test_malformed_json_finishes_rpc_and_rechecks_access_after_sdk_close(
    method: str, revoked_on_close: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    source = _Source()
    source.malformed_method = method
    checked_after_close = False

    async def validate_access() -> None:
        nonlocal checked_after_close
        if source.closed:
            checked_after_close = True
            if revoked_on_close:
                raise MCPSourceError("denied")

    config = MCPSourceConfig(
        _ENDPOINT,
        limits=MCPLimits(timeout_seconds=20, request_seconds=15),
        approved_resource_ids=(_DOCUMENT,),
    )
    client = MCPSourceClient(config, None, validate_access=validate_access, _transport=source)
    code = "denied" if revoked_on_close else "protocol"
    with pytest.raises(MCPSourceError, match=f"^MCP source {code}$"):
        async with asyncio.timeout(3), client:
            assert method == "resources/read"
            await client.read(_DOCUMENT)

    assert source.methods == (
        ["initialize"]
        if method == "initialize"
        else ["initialize", "notifications/initialized", "resources/read"]
    )
    assert source.closed and checked_after_close
    assert _PRIVATE_MARKER not in caplog.text


@pytest.mark.asyncio
async def test_malformed_reply_cannot_replace_existing_wire_limit() -> None:
    source = _Source()
    source.malformed_method = "initialize"
    source.malformed_body = _MALFORMED + b" " * 1024
    config = MCPSourceConfig(_ENDPOINT, limits=MCPLimits(max_wire_bytes=1024))

    with pytest.raises(MCPSourceError, match="^MCP source limit$"):
        async with (
            asyncio.timeout(3),
            MCPSourceClient(config, None, validate_access=_authorized, _transport=source),
        ):
            pytest.fail("a wire limit refusal opened a client session")

    assert source.methods == ["initialize"]
    assert source.closed
