from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import FrozenInstanceError, replace
from typing import Any

import dns.asyncresolver
import httpx
import pytest
from loom_source_worker import mcp_client
from loom_source_worker._mcp_transport import resolve_public_address
from loom_source_worker.direct_backend import ApprovedProfile
from loom_source_worker.main import _parse_settings
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


def test_equivalent_effective_deadlines_share_one_approved_profile_digest() -> None:
    raw: dict[str, Any] = {
        "transport": "tls_proxy_unix",
        "worker_id": "83d061b4-2542-4db7-adc0-4b81b3280c08",
        "issuer": "synthetic-grants",
        "vault": {"origin": "https://vault.example", "mount": "connections"},
        "read_limits": {
            "concurrent": 4,
            "concurrent_per_org": 2,
            "timeout_seconds": 5,
            "content_bytes": 4096,
        },
        "profiles": [{"organizations": ["tenant"], "config": {"endpoint": _ENDPOINT}}],
    }
    omitted = _parse_settings(json.dumps(raw).encode()).profiles[0]
    raw["profiles"][0]["config"]["limits"] = {
        "timeout_seconds": 30.0,
        "request_seconds": 10.0,
    }
    explicit = _parse_settings(json.dumps(raw).encode()).profiles[0]
    direct = ApprovedProfile(
        MCPSourceConfig(_ENDPOINT, limits=MCPLimits(timeout_seconds=30, request_seconds=10)),
        ("tenant",),
    )
    assert omitted.digest == explicit.digest == direct.digest


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
        self.close_started = asyncio.Event()
        self.release_close: asyncio.Event | None = None
        self.stall = False
        self.redirect = False
        self.content_encoding = "identity"
        self.resource_text = "synthetic text"
        self.resource_uri = _DOCUMENT
        self.listed_uri = _DOCUMENT
        self.listed_metadata: dict[str, Any] = {"name": "Synthetic document"}
        self.resource_parts = 1
        self.initialize_error: int | None = None
        self.resource_error: int | None = None
        self.resource_transport_error: httpx.TransportError | None = None
        self.resource_rpc_error = False
        self.resource_extensions: dict[str, Any] = {}
        self.notifications: _IdleBody | None = None
        self.termination: _IdleBody | None = None
        self.change_tools_during_discovery = False

    async def aclose(self) -> None:
        self.close_started.set()
        if self.release_close is not None:
            await self.release_close.wait()
        self.closed = True

    async def respond(self, request: httpx.Request) -> httpx.Response:
        logging.getLogger("mcp.fixture").warning("synthetic private diagnostic")
        if request.method != "POST":
            self.calls.append((request, request.method))
            if request.method == "GET" and self.notifications is not None:
                assert request.extensions["timeout"]["read"] is None
                return httpx.Response(
                    200, stream=self.notifications, headers={"content-type": "text/event-stream"}
                )
            if request.method == "DELETE" and self.termination is not None:
                return httpx.Response(200, stream=self.termination)
            return httpx.Response(405 if request.method == "GET" else 204)
        body = json.loads(request.content)
        method = body["method"]
        self.calls.append((request, method))
        if method == "initialize":
            if self.initialize_error is not None:
                return httpx.Response(self.initialize_error)
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
                    "resources": [{"uri": self.listed_uri, **self.listed_metadata}],
                    "nextCursor": "next",
                }
            )
        elif method == "resources/read":
            self.read_started.set()
            if self.resource_error is not None:
                return httpx.Response(self.resource_error)
            if self.resource_rpc_error:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32603, "message": "synthetic source error"},
                    },
                )
            if self.stall:
                try:
                    await asyncio.Future[None]()
                finally:
                    self.read_cancelled.set()
            result = {
                "contents": [{"uri": self.resource_uri, "text": self.resource_text}]
                * self.resource_parts,
                **self.resource_extensions,
            }
            envelope = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            return httpx.Response(
                200,
                stream=(
                    _Body(f"event: message\ndata: {envelope}\n\n".encode())
                    if self.resource_transport_error is None
                    else _InterruptedBody(self.resource_transport_error)
                ),
                headers={
                    "content-type": "text/event-stream",
                    "content-encoding": self.content_encoding,
                },
            )
        elif method == "tools/list":
            if self.change_tools_during_discovery:
                if not body.get("params", {}).get("cursor"):
                    return _json_response(body["id"], {"tools": [self.tool], "nextCursor": "next"})
                notification = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
                answer = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}}
                frames = "".join(
                    f"event: message\ndata: {json.dumps(item)}\n\n"
                    for item in (notification, answer)
                )
                return httpx.Response(
                    200,
                    stream=_Body(frames.encode()),
                    headers={"content-type": "text/event-stream"},
                )
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


class _InterruptedBody(httpx.AsyncByteStream):
    def __init__(self, failure: httpx.TransportError) -> None:
        self.failure = failure

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"event: message\ndata: "
        raise self.failure


class _IdleBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        yield await asyncio.Future[bytes]()

    async def aclose(self) -> None:
        self.closed = True


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


@pytest.mark.asyncio
async def test_tool_list_notification_invalidates_an_inflight_discovery(pin: list[str]) -> None:
    source, mapping = _mapped_source()
    source.change_tools_during_discovery = True
    async with _client(source, approved_tools=("read",), read=mapping) as client:
        with pytest.raises(MCPSourceError, match="^MCP source schema$"):
            await client.discover()
    assert sum(method == "tools/list" for _, method in source.calls) == 2
    assert not any(method == "tools/call" for _, method in source.calls)


@pytest.mark.asyncio
async def test_advertised_schema_cannot_replace_a_boolean_with_a_number(pin: list[str]) -> None:
    source, mapping = _mapped_source()
    approved = {**mapping.input_schema, "properties": {"id": {"const": True}}}
    changed = {**mapping.input_schema, "properties": {"id": {"const": 1}}}
    assert source.tool is not None
    source.tool["inputSchema"] = changed
    async with _client(
        source, approved_tools=("read",), read=replace(mapping, input_schema=approved)
    ) as client:
        with pytest.raises(MCPSourceError, match="^MCP source schema$"):
            await client.discover()
    assert not any(method == "tools/call" for _, method in source.calls)


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
async def test_stateful_read_accepts_idle_notifications_and_does_not_drain_termination(
    pin: list[str],
) -> None:
    source = _Source()
    source.notifications, source.termination = _IdleBody(), _IdleBody()
    async with (
        asyncio.timeout(2),
        _client(source, approved_resource_ids=(_DOCUMENT,)) as client,
    ):
        await source.notifications.started.wait()
        assert (await client.read(_DOCUMENT)).text == source.resource_text
    assert source.notifications.closed
    assert source.termination.closed and not source.termination.started.is_set()


@pytest.mark.parametrize(
    "identity",
    [
        "https://source.example",
        "https://résumé.example/item",
        "https://user:password@source.example/item",
    ],
)
def test_resource_selection_refuses_a_uri_the_sdk_would_rewrite(identity: str) -> None:
    source = _Source()
    with pytest.raises(MCPSourceError, match="^MCP source unsupported$"):
        _client(source, approved_resource_ids=(identity,))
    assert not source.calls


@pytest.mark.asyncio
async def test_resource_receipt_requires_exact_wire_uri(pin: list[str]) -> None:
    source = _Source()
    source.resource_uri = "https://source.example"
    identity = "https://source.example/"
    async with _client(source, approved_resource_ids=(identity,)) as client:
        with pytest.raises(MCPSourceError, match="^MCP source unsupported$"):
            await client.read(identity)
    assert source.read_started.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parts", "text", "policy"),
    [(2, "a", MCPDataPolicy(max_items=1)), (1, "é" * 9, MCPDataPolicy(max_string_bytes=16))],
)
async def test_resource_content_respects_tightened_data_caps(
    pin: list[str], parts: int, text: str, policy: MCPDataPolicy
) -> None:
    source = _Source()
    source.resource_parts, source.resource_text = parts, text
    async with _client(source, approved_resource_ids=(_DOCUMENT,), data_policy=policy) as client:
        with pytest.raises(MCPSourceError, match="^MCP source limit$"):
            await client.read(_DOCUMENT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extensions", "policy"),
    [
        ({"_meta": {"nested": {"data": {"too": {"deep": True}}}}}, MCPDataPolicy(max_depth=4)),
        ({"extension": "é" * 16}, MCPDataPolicy(max_string_bytes=30)),
        ({"extension": list(range(12))}, MCPDataPolicy(max_nodes=13)),
    ],
)
async def test_resource_parser_bounds_cover_metadata_and_extensions(
    pin: list[str], extensions: dict[str, Any], policy: MCPDataPolicy
) -> None:
    source = _Source()
    source.resource_extensions = extensions
    async with _client(source, approved_resource_ids=(_DOCUMENT,), data_policy=policy) as client:
        with pytest.raises(MCPSourceError, match="^MCP source limit$"):
            await client.read(_DOCUMENT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity", ["https://UPSTREAM.example/path", "https://user:password@source.example/item"]
)
async def test_list_refuses_unsafe_resource_identity(pin: list[str], identity: str) -> None:
    source = _Source()
    source.listed_uri = identity
    async with _client(source) as client:
        with pytest.raises(MCPSourceError, match="^MCP source unsupported$"):
            await client.list_resources()


@pytest.mark.asyncio
async def test_catalog_rejects_a_resource_without_its_required_name(pin: list[str]) -> None:
    source = _Source()
    source.listed_metadata = {"title": "Synthetic title"}
    async with _client(source) as client:
        with pytest.raises(MCPSourceError, match="^MCP source schema$"):
            await client.list_resources()


@pytest.mark.asyncio
async def test_revocation_is_checked_after_a_mapped_tool_error(pin: list[str]) -> None:
    source, mapping = _mapped_source()
    source.tool_result = {"isError": True, "content": [{"type": "text", "text": "private error"}]}

    async def validate_access() -> None:
        if any(method == "tools/call" for _, method in source.calls):
            raise MCPSourceError("denied")

    config = MCPSourceConfig(
        _ENDPOINT, approved_tools=("read",), read=mapping, approved_resource_ids=(_DOCUMENT,)
    )
    async with MCPSourceClient(
        config, None, validate_access=validate_access, _transport=source
    ) as client:
        with pytest.raises(MCPSourceError, match="^MCP source denied$"):
            await client.read(_DOCUMENT)
    assert source.closed


@pytest.mark.asyncio
async def test_revocation_wins_over_initialization_reconnect(pin: list[str]) -> None:
    source = _Source()
    source.initialize_error = 401

    async def validate_access() -> None:
        if any(method == "initialize" for _, method in source.calls):
            raise MCPSourceError("denied")

    config = MCPSourceConfig(_ENDPOINT, approved_resource_ids=(_DOCUMENT,))
    with pytest.raises(MCPSourceError, match="^MCP source denied$"):
        async with MCPSourceClient(
            config, None, validate_access=validate_access, _transport=source
        ):
            pytest.fail("read became available after revocation")
    assert [method for _, method in source.calls] == ["initialize"]
    assert source.closed


@pytest.mark.asyncio
async def test_revocation_wins_over_upstream_reconnect(pin: list[str]) -> None:
    source = _Source()
    source.resource_error = 401

    async def validate_access() -> None:
        if source.read_started.is_set():
            raise MCPSourceError("denied")

    config = MCPSourceConfig(_ENDPOINT, approved_resource_ids=(_DOCUMENT,))
    async with MCPSourceClient(
        config, None, validate_access=validate_access, _transport=source
    ) as client:
        with pytest.raises(MCPSourceError, match="^MCP source denied$"):
            await client.read(_DOCUMENT)
    assert source.closed


@pytest.mark.asyncio
async def test_revocation_after_remote_read_is_reported_without_waiting_for_timeout(
    pin: list[str],
) -> None:
    source = _Source()

    async def validate_access() -> None:
        if source.read_started.is_set():
            raise MCPSourceError("denied")

    config = MCPSourceConfig(_ENDPOINT, approved_resource_ids=(_DOCUMENT,))
    async with (
        asyncio.timeout(2),
        MCPSourceClient(config, None, validate_access=validate_access, _transport=source) as client,
    ):
        with pytest.raises(MCPSourceError, match="^MCP source denied$"):
            await client.read(_DOCUMENT)
    assert source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status"),
    [(httpx.RemoteProtocolError, "unavailable"), (httpx.ReadTimeout, "timeout")],
)
async def test_interrupted_response_retains_transport_status(pin, failure, status) -> None:
    source = _Source()
    source.resource_transport_error = failure("synthetic upstream detail")
    async with _client(source, approved_resource_ids=(_DOCUMENT,)) as client:
        with pytest.raises(MCPSourceError, match=f"^MCP source {status}$"):
            await client.read(_DOCUMENT)
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
@pytest.mark.parametrize("revoked", [False, True])
async def test_cancel_joins_sdk_and_only_terminates_current_session(
    pin: list[str], revoked: bool
) -> None:
    source = _Source()
    source.stall = True

    async def validate_access() -> None:
        if revoked and source.read_started.is_set():
            raise MCPSourceError("denied")

    config = MCPSourceConfig(_ENDPOINT, approved_resource_ids=(_DOCUMENT,))
    async with MCPSourceClient(
        config, None, validate_access=validate_access, _transport=source
    ) as client:
        read = asyncio.create_task(client.read(_DOCUMENT))
        async with asyncio.timeout(3):
            await source.read_started.wait()
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
        assert source.read_cancelled.is_set() and source.closed
        assert sum(method == "DELETE" for _, method in source.calls) == int(not revoked)
        with pytest.raises(MCPSourceError, match="^MCP source closed$"):
            await client.read(_DOCUMENT)


@pytest.mark.asyncio
async def test_established_failure_rechecks_authority_after_sdk_cleanup(pin: list[str]) -> None:
    source = _Source()
    source.resource_rpc_error = True
    source.release_close = asyncio.Event()
    revoked = False
    after_drain = False

    async def current_authority() -> None:
        nonlocal after_drain
        if revoked:
            after_drain = source.closed
            raise MCPSourceError("denied")

    config = MCPSourceConfig(_ENDPOINT, approved_resource_ids=(_DOCUMENT,))
    async with MCPSourceClient(
        config, None, validate_access=current_authority, _transport=source
    ) as client:
        read = asyncio.create_task(client.read(_DOCUMENT))
        try:
            await asyncio.wait_for(source.close_started.wait(), timeout=2)
            revoked = True
        finally:
            source.release_close.set()
        with pytest.raises(MCPSourceError, match="^MCP source denied$"):
            await read
    assert after_drain
    assert [method for _, method in source.calls].count("resources/read") == 1


@pytest.mark.asyncio
async def test_failed_session_delete_rechecks_authority_after_close(pin: list[str]) -> None:
    revoked = False

    class FailingTermination(_Source):
        async def respond(self, request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                self.calls.append((request, request.method))
                return httpx.Response(500)
            return await super().respond(request)

        async def aclose(self) -> None:
            nonlocal revoked
            revoked = True
            await super().aclose()

    source = FailingTermination()
    after_drain = False

    async def current_authority() -> None:
        nonlocal after_drain
        if revoked:
            after_drain = source.closed
            raise MCPSourceError("denied")

    config = MCPSourceConfig(_ENDPOINT, _AUDIENCE, approved_resource_ids=(_DOCUMENT,))
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(
            MCPSourceClient(
                config,
                MCPBearer("synthetic", _AUDIENCE),
                validate_access=current_authority,
                _transport=source,
            )
        )
        assert (await client.read(_DOCUMENT)).text == source.resource_text
        with pytest.raises(MCPSourceError, match="^MCP source denied$"):
            await stack.aclose()

    assert after_drain
    assert [
        (method, request.headers.get("mcp-session-id"))
        for request, method in source.calls
        if method in {"resources/read", "DELETE"}
    ] == [("resources/read", "synthetic-session"), ("DELETE", "synthetic-session")]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["dispatch", "publication"])
async def test_catalog_invalidation_refuses_mapped_read(
    pin: list[str], monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    source, mapping = _mapped_source()
    entered = asyncio.Event()
    release = asyncio.Event()
    dispatch_task: asyncio.Task[Any] | None = None
    operation: asyncio.Task[Any] | None = None
    original_aread = httpx.Request.aread

    async def mark_tool_request(request: httpx.Request) -> bytes:
        data = await original_aread(request)
        if request.method == "POST" and json.loads(data)["method"] == "tools/call":
            nonlocal dispatch_task
            dispatch_task = asyncio.current_task()
        return data

    async def validate_access() -> None:
        current = asyncio.current_task()
        dispatched = any(method == "tools/call" for _, method in source.calls)
        if (stage == "dispatch" and current is dispatch_task and not dispatched) or (
            stage == "publication" and current is operation and dispatched and not entered.is_set()
        ):
            entered.set()
            await release.wait()

    monkeypatch.setattr(httpx.Request, "aread", mark_tool_request)
    config = MCPSourceConfig(
        _ENDPOINT, approved_tools=("read",), read=mapping, approved_resource_ids=(_DOCUMENT,)
    )
    async with MCPSourceClient(
        config, None, validate_access=validate_access, _transport=source
    ) as client:
        operation = asyncio.create_task(client.read(_DOCUMENT))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await client._message_handler(
                mcp_client.types.ServerNotification(mcp_client.types.ToolListChangedNotification())
            )
        finally:
            release.set()
        with pytest.raises(MCPSourceError, match="^MCP source schema$"):
            await operation
    assert sum(method == "tools/call" for _, method in source.calls) == (stage == "publication")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "status", "payload"),
    [
        ("notifications/initialized", 200, b"{}"),
        ("notifications/initialized", 202, b"{}"),
        ("resources/read", 202, b""),
    ],
)
async def test_refuses_invalid_post_response(
    pin: list[str], method: str, status: int, payload: bytes
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, stream=_Body(payload), headers={"content-type": "application/json"}
        )

    message: dict[str, str | int] = {"jsonrpc": "2.0", "method": method}
    if method == "resources/read":
        message["id"] = 1
    transport = mcp_client._PinnedTransport(
        MCPSourceConfig(_ENDPOINT),
        None,
        asyncio.get_running_loop().time() + 1,
        _authorized,
        transport=httpx.MockTransport(respond),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(MCPSourceError, match="^MCP source protocol$"):
            await client.post(_ENDPOINT, json=message)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["file:///local/example", "urn:example:document"])
async def test_native_resources_preserve_exact_hostless_identity(
    pin: list[str], identity: str
) -> None:
    source = _Source()
    source.listed_uri = source.resource_uri = identity
    async with _client(source, approved_resource_ids=(identity,)) as client:
        assert (await client.list_resources()).items[0].resource_id == identity
        item = await client.read(identity)
    assert item.receipt is not None and item.receipt.requested_id == identity
    for request, method in source.calls:
        if method == "resources/read":
            assert json.loads(request.content)["params"]["uri"] == identity


def test_mapped_search_requires_an_items_selector() -> None:
    source, mapping = _mapped_source()
    with pytest.raises(MCPSourceError, match="^MCP source config$"):
        _client(source, approved_tools=(mapping.name,), search=mapping)
    assert not source.calls
