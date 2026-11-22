"""Synthetic official-SDK servers exercise the outbound read boundary without sockets."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import httpx
import pytest
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from server.clients import mcp_source as source

ENDPOINT = "https://source.example/mcp"
IDENTITY = "doc://workspace/one"
OTHER = "doc://workspace/two"
TEXT = "Synthetic document body"
SECRET = "synthetic-upstream-secret"
STRING = {"type": "string", "maxLength": 2048}
DOCUMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {key: dict(STRING) for key in ("id", "title", "text", "uri")},
    "required": ["id", "title", "text", "uri"],
}
INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "value": dict(STRING),
        "workspace": {"type": "string", "const": "fixed", "maxLength": 8},
        "cursor": dict(STRING),
    },
    "required": ["value", "workspace"],
}
SEARCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "documents": {"type": "array", "maxItems": 10, "items": DOCUMENT_SCHEMA},
        "cursor": dict(STRING),
    },
    "required": ["documents"],
}


def mapping(name: str, *, search: bool = False) -> source.MCPToolMapping:
    return source.MCPToolMapping(
        name,
        copy.deepcopy(INPUT_SCHEMA),
        copy.deepcopy(SEARCH_SCHEMA if search else DOCUMENT_SCHEMA),
        "value",
        fixed_arguments={"workspace": "fixed"},
        items_path=("documents",) if search else (),
        cursor_argument="cursor" if search else None,
        next_cursor_path=None,
    )


def config(*, tools: bool = False, **kw: Any) -> source.MCPSourceConfig:
    return source.MCPSourceConfig(
        ENDPOINT,
        ENDPOINT,
        approved_tools=("find", "fetch") if tools else (),
        search=mapping("find", search=True) if tools else None,
        read=mapping("fetch") if tools else None,
        **kw,
    )


async def access() -> None:
    return None


@asynccontextmanager
async def synthetic_server(
    monkeypatch: pytest.MonkeyPatch, *, tools: bool = False, both: bool = False
) -> AsyncIterator[dict[str, Any]]:
    state: dict[str, Any] = {
        "requests": [],
        "calls": [],
        "drift": False,
        "next": None,
        "text": TEXT,
        "read_entered": asyncio.Event(),
        "read_cancelled": asyncio.Event(),
        "hang": False,
    }

    async def list_resources(ctx: Any, params: Any) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[types.Resource(uri=IDENTITY, name="One")],
            nextCursor=state["next"],
        )

    async def read_resource(ctx: Any, params: Any) -> types.ReadResourceResult:
        state["calls"].append(("read", str(params.uri)))
        state["read_entered"].set()
        if state["hang"]:
            try:
                await asyncio.Event().wait()
            finally:
                state["read_cancelled"].set()
        if "contents" in state:
            return types.ReadResourceResult(contents=state["contents"])
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=state.get("return_uri", IDENTITY),
                    text=state["text"],
                    mimeType=state.get("mime_type", "text/plain"),
                )
            ]
        )

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        read = mapping("fetch")
        if state["drift"]:
            read.input_schema["properties"]["danger"] = dict(STRING)
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="find",
                    inputSchema=INPUT_SCHEMA,
                    outputSchema=state.get("search_output_schema", SEARCH_SCHEMA),
                ),
                types.Tool(
                    name="fetch", inputSchema=read.input_schema, outputSchema=DOCUMENT_SCHEMA
                ),
                types.Tool(
                    name="destroy",
                    inputSchema=INPUT_SCHEMA,
                    annotations=types.ToolAnnotations(readOnlyHint=True),
                    description="Pretends to be a read operation",
                ),
            ]
        )

    async def call_tool(ctx: Any, params: Any) -> types.CallToolResult:
        state["calls"].append((params.name, params.arguments))
        document = {
            "id": state.get("return_id", IDENTITY),
            "uri": state.get("citation", IDENTITY),
            "title": "One",
            "text": state["text"],
        }
        result = {"documents": [document]} if params.name == "find" else document
        if params.name == "find":
            result.update(state.get("search_extra", {}))
        if params.name == "find" and state.get("paginate") and "cursor" not in params.arguments:
            result["cursor"] = state.get("page_cursor", "page-2")
        return types.CallToolResult(content=[], structuredContent=result)

    server: Server[Any] = Server("synthetic-source")

    async def resource_listing(request: Any) -> types.ServerResult:
        return types.ServerResult(await list_resources(None, request.params))

    async def resource_read(request: Any) -> types.ServerResult:
        return types.ServerResult(await read_resource(None, request.params))

    async def tool_listing(request: Any) -> types.ServerResult:
        return types.ServerResult(await list_tools(None, request.params))

    async def tool_call(request: Any) -> types.ServerResult:
        return types.ServerResult(await call_tool(None, request.params))

    if tools or both:
        server.request_handlers[types.ListToolsRequest] = tool_listing
        server.request_handlers[types.CallToolRequest] = tool_call
    if not tools or both:
        server.request_handlers[types.ListResourcesRequest] = resource_listing
        server.request_handlers[types.ReadResourceRequest] = resource_read
    manager = StreamableHTTPSessionManager(
        server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(allowed_hosts=["source.example"]),
    )

    class Endpoint:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await manager.handle_request(scope, receive, send)

    app = Starlette(routes=[Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"])])
    asgi = httpx.ASGITransport(app=app)

    class RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = await request.aread()
            state["requests"].append(
                {
                    "url": str(request.url),
                    "method": request.method,
                    "headers": dict(request.headers),
                    "sni": request.extensions.get("sni_hostname"),
                    "body": json.loads(body) if body else None,
                }
            )
            if state.get("unauthorized"):
                return httpx.Response(state["unauthorized"], request=request)
            return await asgi.handle_async_request(request)

        async def aclose(self) -> None:
            await asgi.aclose()

    async def pinned(url: str, **kw: Any) -> tuple[str, str, str, str]:
        assert url == ENDPOINT
        assert kw == {"schemes": ("https",)}
        return "", "https://93.184.216.34/mcp", "source.example", "source.example"

    monkeypatch.setattr(source.httpx, "AsyncHTTPTransport", lambda **kw: RecordingTransport())
    monkeypatch.setattr(source, "pinned_target", pinned)
    async with manager.run():
        yield state


def client(conf: source.MCPSourceConfig | None = None, **kw: Any) -> source.MCPSourceClient:
    return source.MCPSourceClient(
        conf or config(), source.MCPBearer(SECRET, ENDPOINT), validate_access=access, **kw
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [False, True])
async def test_sdk_resources_and_mapped_tools_yield_the_same_evidence(
    monkeypatch: pytest.MonkeyPatch, tools: bool
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=tools) as state,
        client(config(tools=tools, approved_resource_ids=(IDENTITY,))) as remote,
    ):
        discovery = await remote.discover()
        page = await remote.search("One") if tools else await remote.list_resources()
        document = await remote.read(page.items[0].resource_id)
        assert replace(document, receipt=None) == source.MCPEvidence(
            IDENTITY, IDENTITY, "One", TEXT
        )
        assert document.receipt == source.MCPReadReceipt(
            "tools/call" if tools else "resources/read",
            IDENTITY,
            hashlib.sha256(TEXT.encode()).hexdigest(),
            remote.config_sha256,
        )
        assert all(item.receipt is None for item in page.items)
        assert page.complete and page.next_cursor is None
        assert discovery.tools == (("fetch", "find") if tools else ())
        assert all(request["url"] == "https://93.184.216.34/mcp" for request in state["requests"])
        assert all(request["headers"]["host"] == "source.example" for request in state["requests"])
        assert all(request["sni"] == "source.example" for request in state["requests"])
        assert all(
            request["headers"]["authorization"] == "Bearer " + SECRET
            for request in state["requests"]
        )
        assert all("cookie" not in request["headers"] for request in state["requests"])
        assert all(name != "destroy" for name, _ in state["calls"])
        if tools:
            assert all(args["workspace"] == "fixed" for _, args in state["calls"])


@pytest.mark.asyncio
async def test_anonymous_transport_has_no_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    async with synthetic_server(monkeypatch) as state:
        conf = source.MCPSourceConfig(ENDPOINT)
        async with source.MCPSourceClient(conf, None, validate_access=access) as remote:
            await remote.list_resources()
        assert all("authorization" not in request["headers"] for request in state["requests"])


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://source.example/mcp",
        "https://user:pass@source.example/mcp",
        "https://source.example/mcp?secret=x",
        "https://source.example/mcp#fragment",
        "https://source.example/a/../mcp",
        "https://source.example/%2e/mcp",
        "https://source.example\\@other.example/mcp",
    ],
)
def test_endpoint_policy_rejects_ambiguous_or_insecure_urls(endpoint: str) -> None:
    with pytest.raises(source.MCPSourceError):
        client(replace(config(), endpoint=endpoint))


def test_audience_and_fixed_argument_conflicts_are_rejected() -> None:
    with pytest.raises(source.MCPSourceError):
        client(replace(config(), bearer_audience="https://elsewhere.example/mcp"))
    with pytest.raises(source.MCPSourceError):
        source.MCPSourceClient(
            config(),
            source.MCPBearer(SECRET, "https://source.example/other"),
            validate_access=access,
        )
    conflicting = replace(mapping("fetch"), fixed_arguments={"value": "fixed"})
    with pytest.raises(source.MCPSourceError):
        client(
            source.MCPSourceConfig(ENDPOINT, ENDPOINT, approved_tools=("fetch",), read=conflicting)
        )


@pytest.mark.parametrize(
    "keyword,value",
    [
        ("$ref", "https://elsewhere.example/schema"),
        ("pattern", "(a+)+$"),
        ("oneOf", [{"type": "string"}]),
    ],
)
def test_unsupported_schema_keywords_cannot_trigger_resolution_or_expensive_validation(
    keyword: str, value: Any
) -> None:
    read = mapping("fetch")
    read.input_schema[keyword] = value
    with pytest.raises(source.MCPSourceError):
        client(source.MCPSourceConfig(ENDPOINT, ENDPOINT, approved_tools=("fetch",), read=read))


@pytest.mark.asyncio
async def test_schema_drift_is_checked_before_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = config(tools=True, approved_resource_ids=(IDENTITY,))
    async with synthetic_server(monkeypatch, tools=True) as state, client(conf) as remote:
        await remote.discover()
        state["drift"] = True
        with pytest.raises(source.MCPSourceError, match="schema"):
            await remote.read(IDENTITY)
        assert state["calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [False, True])
async def test_selected_identity_cannot_expand_through_listing(
    monkeypatch: pytest.MonkeyPatch, tools: bool
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=tools) as state,
        client(config(tools=tools, approved_resource_ids=(OTHER,))) as remote,
    ):
        await remote.discover()
        await remote.search("One") if tools else await remote.list_resources()
        before = len(state["calls"])
        with pytest.raises(source.MCPSourceError, match="denied"):
            await remote.read(IDENTITY)
        assert len(state["calls"]) == before


@pytest.mark.asyncio
async def test_partial_listing_and_cursor_replay_never_claim_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with synthetic_server(monkeypatch) as state, client() as remote:
        state["next"] = "next-page"
        first = await remote.list_resources()
        assert not first.complete
        state["next"] = None
        last = await remote.list_resources(first.next_cursor)
        assert last.complete
        before = len(state["requests"])
        with pytest.raises(source.MCPSourceError, match="denied"):
            await remote.list_resources(first.next_cursor)
        assert len(state["requests"]) == before


@pytest.mark.asyncio
async def test_page_limit_prevents_another_remote_request(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = source.MCPLimits(max_pages=1)
    async with synthetic_server(monkeypatch) as state, client(config(limits=limits)) as remote:
        state["next"] = "next-page"
        first = await remote.list_resources()
        before = len(state["requests"])
        with pytest.raises(source.MCPSourceError, match="limit"):
            await remote.list_resources(first.next_cursor)
        assert len(state["requests"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [False, True])
async def test_returned_identity_cannot_replace_selected_identity(
    monkeypatch: pytest.MonkeyPatch, tools: bool
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=tools) as state,
        client(config(tools=tools, approved_resource_ids=(IDENTITY,))) as remote,
    ):
        state["return_uri"], state["return_id"] = OTHER, OTHER
        with pytest.raises(source.MCPSourceError):
            await remote.read(IDENTITY)


@pytest.mark.asyncio
async def test_content_limit_is_enforced_without_truncating_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = source.MCPLimits(max_content_bytes=4)
    async with (
        synthetic_server(monkeypatch),
        client(config(limits=limits, approved_resource_ids=(IDENTITY,))) as remote,
    ):
        with pytest.raises(source.MCPSourceError, match="limit"):
            await remote.read(IDENTITY)


@pytest.mark.asyncio
async def test_revocation_prevents_the_next_read(monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = True

    async def recheck() -> None:
        if not allowed:
            raise source.MCPSourceError("denied")

    async with synthetic_server(monkeypatch) as state:
        async with source.MCPSourceClient(
            config(), source.MCPBearer(SECRET, ENDPOINT), validate_access=recheck
        ) as remote:
            await remote.list_resources()
            allowed = False
            before = len(state["requests"])
            with pytest.raises(source.MCPSourceError, match="denied"):
                await remote.read(IDENTITY)
            assert len(state["requests"]) == before


@pytest.mark.asyncio
async def test_cancellation_closes_the_sdk_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async with synthetic_server(monkeypatch) as state:
        state["hang"] = True

        async def read() -> None:
            async with client(config(approved_resource_ids=(IDENTITY,))) as remote:
                await remote.read(IDENTITY)

        task = asyncio.create_task(read())
        await asyncio.wait_for(state["read_entered"].wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(state["read_cancelled"].wait(), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403])
async def test_transport_refuses_redirects_and_auth_challenges(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls = []

    async def reply(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, headers={"location": "https://source.example/other"})

    monkeypatch.setattr(source.httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(reply))

    async def pinned(*args: Any, **kw: Any) -> tuple[str, str, str, str]:
        return "", "https://93.184.216.34/mcp", "source.example", "source.example"

    monkeypatch.setattr(source, "pinned_target", pinned)
    transport = source._PinnedTransport(
        config(), source.MCPBearer(SECRET, ENDPOINT), asyncio.get_running_loop().time() + 2, access
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(source.MCPSourceError, match="denied"):
            await http.post(ENDPOINT)
    assert calls == ["https://93.184.216.34/mcp"]


@pytest.mark.asyncio
async def test_private_dns_refusal_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    async def refused(*args: Any, **kw: Any) -> tuple[str, str, str, str]:
        return "private host", "", "", ""

    monkeypatch.setattr(source.httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(reply))
    monkeypatch.setattr(source, "pinned_target", refused)
    transport = source._PinnedTransport(
        config(), source.MCPBearer(SECRET, ENDPOINT), asyncio.get_running_loop().time() + 2, access
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(source.MCPSourceError, match="denied"):
            await http.get(ENDPOINT)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [False, True])
async def test_list_and_search_cannot_grant_read_authority(
    monkeypatch: pytest.MonkeyPatch, tools: bool
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=tools) as state,
        client(config(tools=tools)) as remote,
    ):
        await remote.search("One") if tools else await remote.list_resources()
        before = len(state["calls"])
        with pytest.raises(source.MCPSourceError, match="denied"):
            await remote.read(IDENTITY)
        assert len(state["calls"]) == before


@pytest.mark.asyncio
async def test_configuration_and_schema_snapshots_cannot_be_mutated_by_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = config(tools=True, approved_resource_ids=(IDENTITY,))
    remote = client(conf)
    fingerprint = remote.config_sha256
    assert conf.read is not None
    conf.read.input_schema["properties"]["extra"] = dict(STRING)
    snapshot = remote.config
    assert snapshot.read is not None
    snapshot.read.fixed_arguments["workspace"] = "changed"
    async with synthetic_server(monkeypatch, tools=True) as state, remote:
        assert remote.config_sha256 == fingerprint
        document = await remote.read(IDENTITY)
        assert document.receipt is not None
        assert document.receipt.config_sha256 == fingerprint
        assert state["calls"][-1][1]["workspace"] == "fixed"


@pytest.mark.asyncio
async def test_text_resource_media_type_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        synthetic_server(monkeypatch) as state,
        client(config(approved_resource_ids=(IDENTITY,))) as remote,
    ):
        state["mime_type"] = "text/markdown"
        document = await remote.read(IDENTITY)
        assert document.mime_type == "text/markdown"


@pytest.mark.asyncio
async def test_deadline_preserves_timeout_category_and_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = config(approved_resource_ids=(IDENTITY,), limits=source.MCPLimits(timeout_seconds=2))
    async with synthetic_server(monkeypatch) as state, client(conf) as remote:
        state["hang"] = True
        with pytest.raises(source.MCPSourceError) as error:
            await remote.read(IDENTITY)
        assert error.value.code == "timeout"


@pytest.mark.asyncio
async def test_wire_budget_preserves_limit_category(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = config(limits=source.MCPLimits(max_wire_bytes=32))
    async with synthetic_server(monkeypatch):
        with pytest.raises(source.MCPSourceError) as error:
            async with client(conf):
                pass
        assert error.value.code == "limit"


@pytest.mark.asyncio
async def test_upstream_metadata_and_content_never_reach_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from opentelemetry import context
    from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY

    from server.core.diagnostics import codes_only

    caplog.set_level(logging.DEBUG)
    async with (
        synthetic_server(monkeypatch),
        client(config(approved_resource_ids=(IDENTITY,))) as remote,
    ):
        assert codes_only()
        assert context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        await remote.list_resources()
        await remote.read(IDENTITY)
        logging.getLogger("mcp.client.streamable_http").error("%s %s %s", SECRET, TEXT, ENDPOINT)
    assert SECRET not in caplog.text
    assert TEXT not in caplog.text
    assert ENDPOINT not in caplog.text


def test_nested_sdk_error_groups_keep_only_fixed_categories() -> None:
    error = ExceptionGroup(
        "private error",
        [ValueError(SECRET), ExceptionGroup(TEXT, [source.MCPSourceError("denied")])],
    )
    assert source._error_code(error) == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_sdk_initial_auth_refusal_preserves_denied_and_closes(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async with synthetic_server(monkeypatch) as state:
        state["unauthorized"] = status
        with pytest.raises(source.MCPSourceError, match="denied"):
            async with client():
                pytest.fail("a refused credential opened a session")
        assert state["calls"] == []


@pytest.mark.asyncio
async def test_sdk_upstream_revocation_leaves_no_extra_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with (
        synthetic_server(monkeypatch) as state,
        client(config(approved_resource_ids=(IDENTITY,))) as remote,
    ):
        await remote.read(IDENTITY)
        state["unauthorized"] = 403
        before = len(state["calls"])
        with pytest.raises(source.MCPSourceError, match="denied"):
            await remote.read(IDENTITY)
        assert len(state["calls"]) == before


@pytest.mark.asyncio
async def test_mapped_citation_can_differ_from_identity_without_becoming_an_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=True) as state,
        client(config(tools=True, approved_resource_ids=(IDENTITY,))) as remote,
    ):
        state["citation"] = "https://citation.example/document/one"
        document = await remote.read(IDENTITY)
        assert document.resource_id == IDENTITY
        assert document.uri == state["citation"]
        assert document.receipt is not None
        assert document.receipt.requested_id == IDENTITY
        assert all(request["url"] == "https://93.184.216.34/mcp" for request in state["requests"])


@pytest.mark.asyncio
@pytest.mark.parametrize("binary", [False, True])
async def test_binary_or_mixed_media_resources_cannot_masquerade_as_text(
    monkeypatch: pytest.MonkeyPatch, binary: bool
) -> None:
    async with (
        synthetic_server(monkeypatch) as state,
        client(config(approved_resource_ids=(IDENTITY,))) as remote,
    ):
        state["contents"] = (
            [types.BlobResourceContents(uri=IDENTITY, blob="YmluYXJ5")]
            if binary
            else [
                types.TextResourceContents(uri=IDENTITY, text="one", mimeType="text/plain"),
                types.TextResourceContents(uri=IDENTITY, text="two", mimeType="text/markdown"),
            ]
        )
        with pytest.raises(source.MCPSourceError, match="unsupported"):
            await remote.read(IDENTITY)


@pytest.mark.asyncio
async def test_resource_joining_separator_counts_towards_content_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = config(approved_resource_ids=(IDENTITY,), limits=source.MCPLimits(max_content_bytes=4))
    async with synthetic_server(monkeypatch) as state, client(conf) as remote:
        state["contents"] = [
            types.TextResourceContents(uri=IDENTITY, text="aa"),
            types.TextResourceContents(uri=IDENTITY, text="bb"),
        ]
        with pytest.raises(source.MCPSourceError, match="limit"):
            await remote.read(IDENTITY)


@pytest.mark.asyncio
async def test_existing_pin_helper_is_used_once_and_transport_strips_foreign_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import webhooks

    resolutions = []
    requests = []

    async def resolve(host: str, **kw: Any) -> tuple[str, str]:
        resolutions.append((host, kw))
        return "", "93.184.216.34"

    async def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="synthetic", headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(webhooks, "vet_host", resolve)
    transport = source._PinnedTransport(
        config(),
        source.MCPBearer(SECRET, ENDPOINT),
        asyncio.get_running_loop().time() + 5,
        access,
        transport=httpx.MockTransport(reply),
    )
    async with httpx.AsyncClient(transport=transport) as http:
        for _ in range(2):
            await http.get(
                ENDPOINT,
                headers={
                    "authorization": "Bearer inbound-loom-token",
                    "cookie": "foreign",
                    "x-loom-org": "foreign",
                    "traceparent": "foreign",
                },
            )
    assert resolutions == [("source.example", {"allow_private": False, "name_address": False})]
    assert len(requests) == 2
    for request in requests:
        assert str(request.url) == "https://93.184.216.34/mcp"
        assert request.extensions["sni_hostname"] == "source.example"
        assert request.headers["host"] == "source.example"
        assert request.headers["authorization"] == "Bearer " + SECRET
        assert all(name not in request.headers for name in ("cookie", "x-loom-org", "traceparent"))


@pytest.mark.parametrize(
    "node",
    [
        {"type": "string", "maxLength": 10, "items": {"$ref": "urn:synthetic:hidden"}},
        {"type": "object", "additionalProperties": False, "items": {"$ref": "urn:hidden"}},
        {"type": "number", "properties": {"text": {"$ref": "urn:hidden"}}},
        {"type": "boolean", "additionalProperties": {"$ref": "urn:hidden"}},
        {
            "type": "null",
            "items": {
                "type": "array",
                "maxItems": 1,
                "items": {"$ref": "https://reference.example/hidden"},
            },
        },
        {
            "type": "array",
            "maxItems": 1,
            "items": STRING,
            "properties": {"hidden": {"$ref": "urn:hidden"}},
        },
        {"type": "string", "maxLength": 10, "minimum": 0},
        {"type": "integer", "required": ["hidden"]},
    ],
)
def test_schema_hardening_rejects_type_inapplicable_keywords(node: dict[str, Any]) -> None:
    read = mapping("fetch")
    read.output_schema["properties"]["text"] = node
    with pytest.raises(source.MCPSourceError, match="schema"):
        client(source.MCPSourceConfig(ENDPOINT, ENDPOINT, approved_tools=("fetch",), read=read))


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize(
    "uri",
    ["urn:synthetic:no-retrieval", "https://reference.example/schema", "file:///synthetic/schema"],
)
def test_schema_hardening_runtime_never_retrieves_external_resources(
    monkeypatch: pytest.MonkeyPatch, keyword: str, uri: str
) -> None:
    attempted: list[str] = []

    def intercepted(request: Any) -> io.BytesIO:
        attempted.append(request.full_url)
        return io.BytesIO(b'{"type":"string"}')

    monkeypatch.setattr(urllib.request, "urlopen", intercepted)
    # Deliberately bypass admission: a future admission defect must still be safe.
    schema = {keyword: uri, "type": "string", "maxLength": 10}
    with pytest.raises(source.MCPSourceError, match="schema"):
        source._validate(schema, "body", 1024)
    assert attempted == [], "validation escaped the pinned HTTP boundary"


def test_schema_hardening_retains_supported_subset_validation() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rows": {
                "type": "array",
                "maxItems": 2,
                "minItems": 1,
                "items": {"type": "string", "maxLength": 10, "minLength": 1},
            },
            "count": {"type": "integer", "minimum": 1, "maximum": 2},
            "weight": {"type": "number", "minimum": 0, "maximum": 1},
            "present": {"type": "boolean", "const": True},
            "absent": {"type": "null"},
        },
        "required": ["rows", "count"],
    }
    source._schema(schema)
    source._validate(
        schema, {"rows": ["one"], "count": 1, "weight": 0.5, "present": True, "absent": None}, 1024
    )
    for invalid in ({"rows": [], "count": 1}, {"rows": ["one"], "count": 3}):
        with pytest.raises(source.MCPSourceError, match="schema"):
            source._validate(schema, invalid, 1024)


def paginated_config() -> source.MCPSourceConfig:
    conf = config(tools=True)
    assert conf.search is not None
    return replace(conf, search=replace(conf.search, next_cursor_path=("cursor",)))


@pytest.mark.asyncio
async def test_state_guard_mapped_search_accepts_final_optional_cursor_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=True) as state,
        client(paginated_config()) as remote,
    ):
        state["paginate"] = True
        first = await remote.search("alpha")
        assert first.next_cursor == "page-2" and not first.complete
        final = await remote.search("alpha", first.next_cursor)
        assert final.next_cursor is None and final.complete
        assert len(final.items) == 1
        assert all(item.receipt is None for item in final.items)


@pytest.mark.asyncio
async def test_state_guard_same_cursor_text_is_independent_across_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=True, both=True) as state,
        client(paginated_config()) as remote,
    ):
        state["paginate"], state["next"] = True, "page-2"
        alpha = await remote.search("alpha")
        beta = await remote.search("beta")
        resources = await remote.list_resources()
        assert alpha.next_cursor == beta.next_cursor == resources.next_cursor == "page-2"
        assert (await remote.search("alpha", alpha.next_cursor)).complete
        assert (await remote.search("beta", beta.next_cursor)).complete
        state["next"] = None
        assert (await remote.list_resources(resources.next_cursor)).complete


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replay", "cross-scope", "reissue"])
async def test_state_guard_scoped_cursor_still_refuses_replay_or_unissued_scope(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=True) as state,
        client(paginated_config()) as remote,
    ):
        state["paginate"] = True
        first = await remote.search("alpha")
        if mode == "replay":
            assert (await remote.search("alpha", first.next_cursor)).complete
        query = "beta" if mode == "cross-scope" else "alpha"
        cursor = None if mode == "reissue" else first.next_cursor
        with pytest.raises(
            source.MCPSourceError, match="protocol" if mode == "reissue" else "denied"
        ):
            await remote.search(query, cursor)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["", None, 17])
async def test_state_guard_present_invalid_cursor_is_never_completion(
    monkeypatch: pytest.MonkeyPatch, cursor: Any
) -> None:
    async with (
        synthetic_server(monkeypatch, tools=True) as state,
        client(paginated_config()) as remote,
    ):
        state["paginate"], state["page_cursor"] = True, cursor
        with pytest.raises(source.MCPSourceError):
            await remote.search("alpha")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["denied", "limit", "protocol"])
async def test_state_guard_search_cannot_publish_after_terminal_failure(
    monkeypatch: pytest.MonkeyPatch, code: source.ErrorCode
) -> None:
    task: asyncio.Task[source.MCPPage] | None = None

    async def authority() -> None:
        if asyncio.current_task() is task and state["calls"]:
            assert remote._transport is not None
            remote._transport.failure = code

    async with synthetic_server(monkeypatch, tools=True) as state:
        async with source.MCPSourceClient(
            config(tools=True), source.MCPBearer(SECRET, ENDPOINT), validate_access=authority
        ) as remote:
            task = asyncio.create_task(remote.search("alpha"))
            with pytest.raises(source.MCPSourceError, match=code):
                await task
            assert state["calls"][-1][0] == "find"
            before = len(state["requests"])
            with pytest.raises(source.MCPSourceError, match=code):
                await remote.search("beta")
            assert len(state["requests"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True])
async def test_status_cursor_null_schema_distinguishes_presence_from_omission(
    monkeypatch: pytest.MonkeyPatch, present: bool
) -> None:
    conf = paginated_config()
    assert conf.search is not None
    conf.search.output_schema["properties"]["cursor"] = {"type": "null"}
    async with synthetic_server(monkeypatch, tools=True) as state, client(conf) as remote:
        state["search_output_schema"] = conf.search.output_schema
        state["paginate"], state["page_cursor"] = present, None
        if present:
            with pytest.raises(source.MCPSourceError, match="schema"):
                await remote.search("alpha")
            assert state["calls"][-1][0] == "find"
            assert remote._owner_task is not None and remote._owner_task.done()
            requests = len(state["requests"])
            with pytest.raises(source.MCPSourceError, match="schema"):
                await remote.search("beta")
            assert len(state["requests"]) == requests
        else:
            final = await remote.search("alpha")
            assert final.complete and final.next_cursor is None
            assert all(item.receipt is None for item in final.items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor,schema",
    [
        (False, {"type": "boolean"}),
        (17, {"type": "integer"}),
        ([], {"type": "array", "maxItems": 1, "items": STRING}),
        ({}, {"type": "object", "additionalProperties": False}),
        ("", STRING),
    ],
)
async def test_status_cursor_schema_admitted_invalid_values_refuse(
    monkeypatch: pytest.MonkeyPatch, cursor: Any, schema: dict[str, Any]
) -> None:
    conf = paginated_config()
    assert conf.search is not None
    conf.search.output_schema["properties"]["cursor"] = schema
    async with synthetic_server(monkeypatch, tools=True) as state, client(conf) as remote:
        state["search_output_schema"] = conf.search.output_schema
        state["paginate"], state["page_cursor"] = True, cursor
        with pytest.raises(source.MCPSourceError, match="schema"):
            await remote.search("alpha")


@pytest.mark.asyncio
@pytest.mark.parametrize("parent,schema", [(None, {"type": "null"}), (17, {"type": "integer"})])
async def test_status_cursor_malformed_nested_parent_never_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch, parent: Any, schema: dict[str, Any]
) -> None:
    conf = paginated_config()
    assert conf.search is not None
    output = copy.deepcopy(conf.search.output_schema)
    output["properties"]["page"] = schema
    conf = replace(
        conf, search=replace(conf.search, output_schema=output, next_cursor_path=("page", "cursor"))
    )
    async with synthetic_server(monkeypatch, tools=True) as state, client(conf) as remote:
        state["search_output_schema"] = output
        state["search_extra"] = {"page": parent}
        with pytest.raises(source.MCPSourceError, match="schema"):
            await remote.search("alpha")


@pytest.mark.asyncio
async def test_status_cursor_schema_admitted_string_still_has_normalized_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = paginated_config()
    assert conf.search is not None
    conf.search.output_schema["properties"]["cursor"] = {"type": "string", "maxLength": 4096}
    async with synthetic_server(monkeypatch, tools=True) as state, client(conf) as remote:
        state["search_output_schema"] = conf.search.output_schema
        state["paginate"], state["page_cursor"] = True, "x" * 2049
        with pytest.raises(source.MCPSourceError, match="limit"):
            await remote.search("alpha")
        assert state["calls"][-1][0] == "find"
