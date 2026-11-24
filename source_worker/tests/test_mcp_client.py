from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, replace
from typing import Any

import dns.asyncresolver
import httpx
import pytest
from loom_source_worker import mcp_client
from loom_source_worker._mcp_transport import resolve_public_address
from loom_source_worker.mcp_client import (
    MCPBearer,
    MCPDataPolicy,
    MCPLimits,
    MCPSourceClient,
    MCPSourceConfig,
    MCPSourceError,
    MCPToolMapping,
    validate_mcp_tool_mapping,
)

_ENDPOINT = "https://source.example:8443/mcp"
_AUDIENCE = "https://source.example:8443/resource"
_DOCUMENT = "mcp://fixture/document"


async def _authorized() -> None:
    return None


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_resolver() -> None:
        pytest.fail("unexpected DNS lookup")

    monkeypatch.setattr(dns.asyncresolver, "Resolver", refuse_resolver)


@pytest.fixture
def pin(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    resolutions: list[str] = []

    async def resolve(host: str) -> ipaddress.IPv6Address:
        resolutions.append(host)
        return ipaddress.IPv6Address("2600::10")

    monkeypatch.setattr(mcp_client, "resolve_public_address", resolve)
    return resolutions


class _Source(httpx.MockTransport):
    def __init__(self) -> None:
        super().__init__(self.respond)
        self.calls: list[tuple[httpx.Request, str]] = []
        self.tool: dict[str, Any] | None = None
        self.tool_result: dict[str, Any] = {"content": []}
        self.read_started = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.closed = False
        self.stall = False
        self.redirect = False
        self.content_encoding = "identity"
        self.resource_text = "synthetic text"

    async def aclose(self) -> None:
        self.closed = True

    async def respond(self, request: httpx.Request) -> httpx.Response:
        logging.getLogger("mcp.fixture").warning("synthetic private diagnostic")
        if request.method != "POST":
            self.calls.append((request, request.method))
            return httpx.Response(405 if request.method == "GET" else 204)
        body = json.loads(request.content)
        method = body["method"]
        self.calls.append((request, method))
        if method == "initialize":
            if self.redirect:
                return httpx.Response(307, headers={"location": "https://other.example/mcp"})
            capabilities = {"resources": {}} if self.tool is None else {"tools": {}}
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": capabilities,
                "serverInfo": {"name": "fixture", "version": "1"},
            }
            return _json_response(body["id"], result, **{"mcp-session-id": "synthetic-session"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "resources/list":
            result = (
                {"resources": []}
                if body.get("params", {}).get("cursor") == "next"
                else {
                    "resources": [{"uri": _DOCUMENT, "name": "Synthetic document"}],
                    "nextCursor": "next",
                }
            )
        elif method == "resources/read":
            self.read_started.set()
            if self.stall:
                try:
                    await asyncio.Future[None]()
                finally:
                    self.read_cancelled.set()
            result = {"contents": [{"uri": _DOCUMENT, "text": self.resource_text}]}
            envelope = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            return httpx.Response(
                200,
                stream=_Body(f"event: message\ndata: {envelope}\n\n".encode()),
                headers={
                    "content-type": "text/event-stream",
                    "content-encoding": self.content_encoding,
                },
            )
        elif method == "tools/list":
            result = {"tools": [self.tool]}
        elif method == "tools/call":
            result = self.tool_result
        else:
            pytest.fail(f"unexpected RPC: {method}")
        return _json_response(body["id"], result)


class _Body(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.content[:64]
        yield self.content[64:]


def _json_response(request_id: int, result: dict[str, Any], **headers: str) -> httpx.Response:
    content = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()
    return httpx.Response(
        200, stream=_Body(content), headers={"content-type": "application/json", **headers}
    )


def _client(source: _Source, **config: Any) -> MCPSourceClient:
    return MCPSourceClient(
        MCPSourceConfig(_ENDPOINT, **config), None, validate_access=_authorized, _transport=source
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), [(401, "reconnect"), (403, "denied")])
async def test_preserves_the_upstream_auth_refusal(status: int, code: str, pin: list[str]) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="private provider diagnostic")

    client = MCPSourceClient(
        MCPSourceConfig(_ENDPOINT),
        None,
        validate_access=_authorized,
        _transport=httpx.MockTransport(refuse),
    )
    with pytest.raises(MCPSourceError, match=f"^MCP source {code}$"):
        async with client:
            pytest.fail("authentication refusal opened a client session")
    assert pin == ["source.example"]


@pytest.mark.asyncio
async def test_resource_read_pins_origin_and_sends_only_exact_audience_bearer(
    pin: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    source = _Source()
    config = MCPSourceConfig(_ENDPOINT, _AUDIENCE, approved_resource_ids=(_DOCUMENT,))
    with pytest.raises(MCPSourceError, match="^MCP source config$"):
        MCPSourceClient(config, MCPBearer("synthetic", _ENDPOINT), validate_access=_authorized)
    client = MCPSourceClient(
        config, MCPBearer("synthetic", _AUDIENCE), validate_access=_authorized, _transport=source
    )
    async with client:
        assert (await client.discover()).resources
        first = await client.list_resources()
        assert not first.complete and first.next_cursor == "next"
        assert (await client.list_resources(first.next_cursor)).complete
        item = await client.read(_DOCUMENT)
        assert item.title == "Synthetic document"
        assert item.receipt is not None
        assert item.receipt.requested_id == _DOCUMENT
        assert item.receipt.content_sha256 == hashlib.sha256(b"synthetic text").hexdigest()
        assert item.receipt.config_sha256 == client.config_sha256
        assert item.receipt.identity_verified
    assert pin == ["source.example"]
    assert source.closed
    assert "synthetic private diagnostic" not in caplog.text
    logging.getLogger("mcp.fixture").warning("synthetic public diagnostic")
    assert "synthetic public diagnostic" in caplog.text
    for request, _method in source.calls:
        assert str(request.url) == "https://[2600::10]:8443/mcp"
        assert request.headers["host"] == "source.example:8443"
        assert request.headers["authorization"] == "Bearer synthetic"
        assert request.extensions["sni_hostname"] == "source.example"


@pytest.mark.asyncio
async def test_redirect_is_not_followed(pin: list[str]) -> None:
    source = _Source()
    source.redirect = True
    with pytest.raises(MCPSourceError, match="^MCP source denied$"):
        async with _client(source):
            pytest.fail("redirect accepted")
    assert pin == ["source.example"]
    assert [method for _, method in source.calls] == ["initialize"]
    assert "authorization" not in source.calls[0][0].headers
    assert source.closed


@pytest.mark.asyncio
async def test_unapproved_resource_id_poison_session(pin: list[str]) -> None:
    source = _Source()
    async with _client(source) as client:
        first = await client.list_resources()
        with pytest.raises(MCPSourceError, match="^MCP source denied$"):
            await client.read(first.items[0].resource_id)
        with pytest.raises(MCPSourceError, match="^MCP source denied$"):
            await client.list_resources()
    assert "resources/read" not in [method for _, method in source.calls]


@pytest.mark.asyncio
async def test_dns_rejects_mixed_public_and_nonpublic_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Record:
        def __init__(self, address: str) -> None:
            self.address = address

        def to_text(self) -> str:
            return self.address

    class Resolver:
        async def resolve(self, host: str, family: str, **options: Any) -> list[Record]:
            assert host == "source.example"
            assert options == {"search": False, "raise_on_no_answer": False}
            return [Record("8.8.8.8" if family == "A" else "64:ff9b::7f00:1")]

    monkeypatch.setattr(dns.asyncresolver, "Resolver", Resolver)
    assert await resolve_public_address("source.example") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::ffff:127.0.0.1", "::127.0.0.1", "ff02::1"])
async def test_nonpublic_literals_are_refused(host: str) -> None:
    assert await resolve_public_address(host) is None


def _mapped_source() -> tuple[_Source, MCPToolMapping]:
    input_schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    output_schema = {"type": "object"}
    mapping = MCPToolMapping("read", input_schema, output_schema, "id")
    source = _Source()
    source.tool = {"name": "read", "inputSchema": input_schema, "outputSchema": output_schema}
    source.tool_result = {
        "content": [],
        "structuredContent": {"id": _DOCUMENT, "title": "title", "text": "text", "uri": _DOCUMENT},
    }
    return source, mapping


@pytest.mark.asyncio
async def test_mapping_snapshots_approved_schemas_and_refuses_drift(pin: list[str]) -> None:
    source, mapping = _mapped_source()
    async with _client(
        source, approved_tools=("read",), read=mapping, approved_resource_ids=(_DOCUMENT,)
    ) as client:
        assert (await client.discover()).tools == ("read",)
        item = await client.read(_DOCUMENT)
        assert item.receipt is not None and item.receipt.operation == "tools/call"
        assert source.tool is not None
        mapping.output_schema["additionalProperties"] = False
        with pytest.raises(MCPSourceError, match="^MCP source schema$"):
            await client.read(_DOCUMENT)
    assert sum(method == "tools/call" for _, method in source.calls) == 1


@pytest.mark.parametrize(
    ("policy", "value"),
    [
        (MCPDataPolicy(max_nodes=13), list(range(12))),
        (MCPDataPolicy(max_depth=4), [[[["deep"]]]]),
        (MCPDataPolicy(max_items=1), [1, 2]),
        (MCPDataPolicy(max_string_bytes=24), "é" * 13),
    ],
)
def test_parser_policy_bounds_fixed_arguments(policy: MCPDataPolicy, value: Any) -> None:
    mapping = MCPToolMapping("read", {"type": "object"}, {"type": "object"}, "id")
    validate_mcp_tool_mapping(mapping, policy=policy)
    with pytest.raises(MCPSourceError, match="^MCP source limit$"):
        validate_mcp_tool_mapping(replace(mapping, fixed_arguments={"test": value}), policy=policy)


def test_parser_policy_is_immutable_and_cannot_raise_ceilings() -> None:
    policy = MCPDataPolicy()
    with pytest.raises(FrozenInstanceError):
        policy.max_depth = 17
    for invalid in (
        {"max_depth": 17},
        {"max_items": True},
        {"max_string_bytes": 0},
        {"max_nodes": 4097},
    ):
        with pytest.raises(MCPSourceError, match="^MCP source config$"):
            replace(policy, **invalid)


@pytest.mark.asyncio
async def test_tool_response_is_bounded_even_with_permissive_schema(pin: list[str]) -> None:
    source, mapping = _mapped_source()
    source.tool_result["structuredContent"]["extra"] = [1, 2]
    async with _client(
        source,
        approved_tools=("read",),
        read=mapping,
        approved_resource_ids=(_DOCUMENT,),
        data_policy=MCPDataPolicy(max_items=1),
    ) as client:
        with pytest.raises(MCPSourceError, match="^MCP source limit$"):
            await client.read(_DOCUMENT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "encoding", "error"),
    [
        (MCPLimits(max_wire_bytes=1024, request_seconds=1), "identity", "limit"),
        (MCPLimits(request_seconds=1), "gzip", "protocol"),
    ],
)
async def test_stream_rejects_over_budget_or_compressed_data(
    pin: list[str], limits: MCPLimits, encoding: str, error: str
) -> None:
    source = _Source()
    source.content_encoding = encoding
    source.resource_text = "x" * 1024
    async with _client(source, approved_resource_ids=(_DOCUMENT,), limits=limits) as client:
        with pytest.raises(MCPSourceError, match=f"^MCP source {error}$"):
            await client.read(_DOCUMENT)
    assert source.closed


@pytest.mark.asyncio
async def test_revalidation_refusal_prevents_more_requests(pin: list[str]) -> None:
    source = _Source()
    revoked = asyncio.Event()

    async def validate_access() -> None:
        if revoked.is_set():
            raise MCPSourceError("denied")

    config = MCPSourceConfig(_ENDPOINT, approved_resource_ids=(_DOCUMENT,))
    async with MCPSourceClient(
        config, None, validate_access=validate_access, _transport=source
    ) as client:
        await client.list_resources()
        revoked.set()
        with pytest.raises(MCPSourceError, match="^MCP source denied$"):
            await client.read(_DOCUMENT)
    assert "resources/read" not in [method for _, method in source.calls]
    assert source.closed


@pytest.mark.asyncio
async def test_session_deadline_cancels_stalled_read(pin: list[str]) -> None:
    source = _Source()
    source.stall = True
    limits = MCPLimits(timeout_seconds=1, request_seconds=3)
    async with _client(source, approved_resource_ids=(_DOCUMENT,), limits=limits) as client:
        with pytest.raises(MCPSourceError, match="^MCP source timeout$"):
            await client.read(_DOCUMENT)
    assert source.read_started.is_set() and source.read_cancelled.is_set() and source.closed


@pytest.mark.asyncio
async def test_cancel_drains_owned_sdk_request_before_return(pin: list[str]) -> None:
    source = _Source()
    source.stall = True
    async with _client(source, approved_resource_ids=(_DOCUMENT,)) as client:
        read = asyncio.create_task(client.read(_DOCUMENT))
        async with asyncio.timeout(3):
            await source.read_started.wait()
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
        assert source.read_cancelled.is_set() and source.closed
        with pytest.raises(MCPSourceError, match="^MCP source closed$"):
            await client.read(_DOCUMENT)
