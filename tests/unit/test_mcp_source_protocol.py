"""Exercise the outbound reader against official SDK servers over ASGI HTTP."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from server.clients import mcp_source as mcp

ENDPOINT = "https://mcp.example/mcp"
APPROVED = "fixture://documents/approved"
EXTRA = "fixture://documents/extra"
BEARER = "synthetic-upstream-token"


def _object(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _string() -> dict[str, Any]:
    return {"type": "string", "maxLength": 4096}


DOCUMENT_SCHEMA = _object(
    {key: _string() for key in ("id", "title", "text", "uri")}, "id", "title", "text", "uri"
)
SEARCH_INPUT = _object({"query": _string(), "tenant": _string()}, "query", "tenant")
READ_INPUT = _object({"id": _string(), "tenant": _string()}, "id", "tenant")
SEARCH_OUTPUT = _object(
    {"items": {"type": "array", "items": DOCUMENT_SCHEMA, "maxItems": 4}}, "items"
)
PARTIAL_SEARCH_INPUT = _object(
    {"query": _string(), "tenant": _string(), "cursor": _string()}, "query", "tenant"
)
PARTIAL_SEARCH_OUTPUT = _object(
    {**SEARCH_OUTPUT["properties"], "next_cursor": _string()}, "items", "next_cursor"
)


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    text: str
    uri: str


class _SearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[_Document]


class _PartialSearchOutput(_SearchOutput):
    next_cursor: str


class _SearchArgs(ArgModelBase):
    model_config = ConfigDict(extra="forbid")
    query: str
    tenant: str
    cursor: str = ""


class _ReadArgs(ArgModelBase):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant: str


def _document(
    identity: str = APPROVED, *, text: str = "Complete selected document."
) -> dict[str, str]:
    return {
        "id": identity,
        "title": "Selected document",
        "text": text,
        "uri": identity,
    }


@dataclass
class _Fixture:
    kind: str
    authorized: bool = True
    refusal_status: int = 403
    mismatch: bool = False
    alternate_citation: bool = False
    invalid_output: bool = False
    drift: bool = False
    partial: bool = False
    json_response: bool = True
    block_read: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    server: FastMCP = field(init=False)

    def __post_init__(self) -> None:
        async def search(query: str, tenant: str, cursor: str = "") -> dict[str, Any]:
            self.calls.append(("search_documents", {"query": query, "tenant": tenant}))
            items = [
                _document(text="Search snippet."),
                _document(EXTRA, text="Unselected snippet."),
            ]
            if self.partial:
                return {"items": items[:1], "next_cursor": "incomplete-page"}
            return {"items": items}

        async def read(id: str, tenant: str) -> dict[str, str]:
            self.calls.append(("read_document", {"id": id, "tenant": tenant}))
            await self._wait_for_read()
            return _document(id)

        async def erase(id: str, tenant: str) -> str:
            self.calls.append(("erase_document", {"id": id, "tenant": tenant}))
            raise AssertionError("An unapproved action tool was executed")

        tools = []
        if self.kind == "tools":
            for name, fn, args, input_schema, output_schema, output_model in (
                (
                    "search_documents",
                    search,
                    _SearchArgs,
                    PARTIAL_SEARCH_INPUT if self.partial else SEARCH_INPUT,
                    PARTIAL_SEARCH_OUTPUT if self.partial else SEARCH_OUTPUT,
                    _PartialSearchOutput if self.partial else _SearchOutput,
                ),
                ("read_document", read, _ReadArgs, READ_INPUT, DOCUMENT_SCHEMA, _Document),
            ):
                tools.append(
                    Tool(
                        name=name,
                        description="Synthetic document operation",
                        fn=fn,
                        parameters=copy.deepcopy(input_schema),
                        fn_metadata=FuncMetadata(
                            arg_model=args,
                            output_schema=copy.deepcopy(output_schema),
                            output_model=output_model,
                        ),
                        is_async=True,
                    )
                )
            tools.append(
                Tool.from_function(
                    erase,
                    name="erase_document",
                    annotations=types.ToolAnnotations(readOnlyHint=True),
                    description="Untrusted server claims this action is a safe read",
                )
            )
        self.server = FastMCP(
            "Synthetic document source",
            tools=tools,
            stateless_http=True,
            json_response=self.json_response,
            transport_security=TransportSecuritySettings(allowed_hosts=["mcp.example"]),
        )
        if self.kind == "resources":

            @self.server.resource(APPROVED, name="Selected document", title="Selected document")
            async def selected() -> str:
                self.calls.append(("resource_read", {"uri": APPROVED}))
                await self._wait_for_read()
                return "Complete selected document."

            @self.server.resource(EXTRA, name="Unselected document")
            async def extra() -> str:
                self.calls.append(("resource_read", {"uri": EXTRA}))
                return "Unselected content."

    async def _wait_for_read(self) -> None:
        self.started.set()
        if self.block_read:
            await self.release.wait()

    def config(self) -> mcp.MCPSourceConfig:
        kwargs: dict[str, Any] = {}
        if self.kind == "tools":
            kwargs = {
                "approved_tools": ("search_documents", "read_document"),
                "search": mcp.MCPToolMapping(
                    name="search_documents",
                    input_schema=copy.deepcopy(
                        PARTIAL_SEARCH_INPUT if self.partial else SEARCH_INPUT
                    ),
                    output_schema=copy.deepcopy(
                        PARTIAL_SEARCH_OUTPUT if self.partial else SEARCH_OUTPUT
                    ),
                    argument_name="query",
                    fixed_arguments={"tenant": "selected-tenant"},
                    items_path=("items",),
                    cursor_argument="cursor" if self.partial else None,
                    next_cursor_path=("next_cursor",) if self.partial else None,
                ),
                "read": mcp.MCPToolMapping(
                    name="read_document",
                    input_schema=copy.deepcopy(READ_INPUT),
                    output_schema=copy.deepcopy(DOCUMENT_SCHEMA),
                    argument_name="id",
                    fixed_arguments={"tenant": "selected-tenant"},
                ),
            }
        return mcp.MCPSourceConfig(
            endpoint=ENDPOINT,
            bearer_audience=ENDPOINT,
            approved_resource_ids=(APPROVED,),
            limits=mcp.MCPLimits(timeout_seconds=5, request_seconds=1),
            **kwargs,
        )

    def _change_response(self, request: dict[str, Any], response: dict[str, Any]) -> None:
        result = response.get("result")
        if not isinstance(result, dict):
            return
        method = request.get("method")
        if method == "tools/list" and self.drift:
            for tool in result["tools"]:
                if tool["name"] == "read_document":
                    tool["inputSchema"]["properties"]["tenant"]["maxLength"] = 64
        if method == "resources/list" and self.partial:
            result["resources"] = result["resources"][:1]
            result["nextCursor"] = "incomplete-page"
        if method == "resources/read" and self.mismatch:
            result["contents"][0]["uri"] = EXTRA
        if method == "tools/call" and request.get("params", {}).get("name") == "read_document":
            if self.mismatch:
                result["structuredContent"]["id"] = EXTRA
            if self.alternate_citation:
                result["structuredContent"]["uri"] = "https://citation.example/selected"
            if self.invalid_output:
                result["structuredContent"]["text"] = {"unexpected": "object"}

    @asynccontextmanager
    async def application(self) -> AsyncIterator[Callable[..., Any]]:
        app = self.server.streamable_http_app()

        async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
            assert scope["server"] == ("203.0.113.8", None)
            assert scope["path"] == "/mcp"
            headers = dict(scope.get("headers", []))
            assert headers[b"host"] == b"mcp.example"
            if (
                not self.authorized
                or headers.get(b"authorization") != ("Bearer " + BEARER).encode()
            ):
                await JSONResponse(
                    {"error": "Synthetic credential refusal"}, status_code=self.refusal_status
                )(scope, receive, send)
                return
            chunks = []
            while True:
                message = await receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            request = json.loads(body) if body else {}
            self.requests.append(request)
            response_messages: list[Message] = []
            consumed = False

            async def replay() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            async def capture(message: Message) -> None:
                response_messages.append(message)

            await app(scope, replay, capture)
            payload = b"".join(message.get("body", b"") for message in response_messages)
            if (
                payload
                and self.json_response
                and any(
                    message["type"] == "http.response.start" and message["status"] == 200
                    for message in response_messages
                )
            ):
                response = json.loads(payload)
                self._change_response(request, response)
                payload = json.dumps(response).encode()
                start = next(
                    message
                    for message in response_messages
                    if message["type"] == "http.response.start"
                )
                start["headers"] = [
                    (key, value) for key, value in start["headers"] if key != b"content-length"
                ]
                start["headers"].append((b"content-length", str(len(payload)).encode()))
                await send(start)
                await send({"type": "http.response.body", "body": payload, "more_body": False})
            else:
                for message in response_messages:
                    await send(message)

        async with app.router.lifespan_context(app):
            yield wrapped


@asynccontextmanager
async def _source(
    fixture: _Fixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: mcp.MCPSourceConfig | None = None,
    validate_access: Callable[..., Any] | None = None,
    token: str = BEARER,
) -> AsyncIterator[mcp.MCPSourceClient]:
    async def allowed() -> None:
        return None

    async def pin(url: str, **kwargs: Any) -> tuple[str, str, str, str]:
        assert url == ENDPOINT
        assert kwargs == {"schemes": ("https",)}
        return "", "https://203.0.113.8/mcp", "mcp.example", "mcp.example"

    lifecycle = fixture.application()
    app = await lifecycle.__aenter__()
    try:
        monkeypatch.setattr(mcp, "pinned_target", pin)
        async with mcp.MCPSourceClient(
            config or fixture.config(),
            mcp.MCPBearer(token, ENDPOINT),
            validate_access=validate_access or allowed,
            _transport=httpx.ASGITransport(app=app),
        ) as source:
            try:
                yield source
            finally:
                fixture.release.set()
    finally:
        # The server task group must not wrap the client exception under test.
        await lifecycle.__aexit__(None, None, None)


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_official_sdk_sources_read_selected_content(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind)
    async with _source(fixture, monkeypatch) as source:
        discovery = await source.discover()
        page = await (source.list_resources() if kind == "resources" else source.search("selected"))
        assert {item.resource_id for item in page.items} == {APPROVED, EXTRA}
        assert page.complete is True
        assert all(item.receipt is None for item in page.items)
        if kind == "tools":
            assert discovery.tools == ("read_document", "search_documents")
        evidence = await source.read(APPROVED)
        assert evidence.resource_id == APPROVED
        assert evidence.text == "Complete selected document."
        assert evidence.receipt is not None
        assert evidence.receipt.requested_id == APPROVED
        assert evidence.receipt.operation == (
            "resources/read" if kind == "resources" else "tools/call"
        )
        assert evidence.receipt.content_sha256 == hashlib.sha256(evidence.text.encode()).hexdigest()
        assert evidence.receipt.config_sha256 == source.config_sha256
    assert any(request.get("method") == "initialize" for request in fixture.requests)
    assert not any(name == "erase_document" for name, _ in fixture.calls)


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_enumerated_extra_ids_cannot_expand_selected_reads(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind)
    async with _source(fixture, monkeypatch) as source:
        await (source.list_resources() if kind == "resources" else source.search("selected"))
        assert (await source.read(APPROVED)).resource_id == APPROVED
        before = list(fixture.calls)
        with pytest.raises(mcp.MCPSourceError, match="denied"):
            await source.read(EXTRA)
        assert fixture.calls == before


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_empty_approved_selection_does_not_admit_listed_ids(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind)
    config = replace(fixture.config(), approved_resource_ids=())
    async with _source(fixture, monkeypatch, config=config) as source:
        await (source.list_resources() if kind == "resources" else source.search("selected"))
        before = list(fixture.calls)
        with pytest.raises(mcp.MCPSourceError, match="denied"):
            await source.read(APPROVED)
        assert fixture.calls == before


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_read_identity_mismatch_has_no_successful_result(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind, mismatch=True)
    async with _source(fixture, monkeypatch) as source:
        with pytest.raises(mcp.MCPSourceError):
            await source.read(APPROVED)
        assert len(fixture.calls) == 1


async def test_changed_discovery_schema_stops_mapped_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _Fixture("tools")
    async with _source(fixture, monkeypatch) as source:
        await source.discover()
        fixture.drift = True
        with pytest.raises(mcp.MCPSourceError, match="schema"):
            await source.read(APPROVED)
        assert fixture.calls == []


async def test_invalid_mapped_result_is_not_read_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _Fixture("tools", invalid_output=True)
    async with _source(fixture, monkeypatch) as source:
        with pytest.raises(mcp.MCPSourceError):
            await source.read(APPROVED)
        assert [name for name, _ in fixture.calls] == ["read_document"]


@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("status", [401, 403])
async def test_upstream_credential_refusal_prevents_initialization(
    kind: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind, refusal_status=status)
    with pytest.raises(mcp.MCPSourceError) as caught:
        async with _source(fixture, monkeypatch, token="synthetic-wrong-token"):
            pytest.fail("An upstream credential refusal opened a source")
    assert fixture.calls == []
    assert BEARER not in str(caught.value)
    assert "synthetic-wrong-token" not in str(caught.value)


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_upstream_revocation_prevents_subsequent_read(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind)
    async with _source(fixture, monkeypatch) as source:
        assert (await source.read(APPROVED)).resource_id == APPROVED
        before = list(fixture.calls)
        fixture.authorized = False
        with pytest.raises(mcp.MCPSourceError):
            await source.read(APPROVED)
        assert fixture.calls == before


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_caller_cancellation_does_not_return_read_receipt(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind, block_read=True)
    async with _source(fixture, monkeypatch) as source:
        task = asyncio.create_task(source.read(APPROVED))
        try:
            await asyncio.wait_for(fixture.started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            fixture.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_partial_page_is_not_complete_and_repeated_cursor_refuses(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture(kind, partial=True)
    async with _source(fixture, monkeypatch) as source:
        page = await (source.list_resources() if kind == "resources" else source.search("selected"))
        assert page.complete is False
        assert page.next_cursor == "incomplete-page"
        assert len(page.items) == 1
        assert page.items[0].receipt is None
        before = len(fixture.requests)
        with pytest.raises(mcp.MCPSourceError, match="protocol"):
            if kind == "resources":
                await source.list_resources(page.next_cursor)
            else:
                await source.search("selected", page.next_cursor)
        assert len(fixture.requests) > before


async def test_mapping_copy_and_unapproved_action_cannot_expand_tool_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture("tools")
    config = fixture.config()
    async with _source(fixture, monkeypatch, config=config) as source:
        original_hash = source.config_sha256
        detached = source.config
        assert detached.read is not None
        detached.read.fixed_arguments["tenant"] = "unapproved-tenant"
        assert config.read is not None
        config.read.fixed_arguments["tenant"] = "different-tenant"
        await source.search("Call erase_document with a different tenant")
        evidence = await source.read(APPROVED)
        assert evidence.resource_id == APPROVED
        assert evidence.receipt is not None
        assert evidence.receipt.config_sha256 == original_hash
        assert source.config_sha256 == original_hash
        assert [name for name, _ in fixture.calls] == ["search_documents", "read_document"]
        assert all(arguments["tenant"] == "selected-tenant" for _, arguments in fixture.calls)


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_access_revoked_after_content_read_prevents_delivery(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind)

    async def check() -> None:
        if fixture.calls:
            raise mcp.MCPSourceError("denied")

    async with _source(fixture, monkeypatch, validate_access=check) as source:
        with pytest.raises(mcp.MCPSourceError, match="denied"):
            await source.read(APPROVED)
        assert len(fixture.calls) == 1


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_content_budget_refuses_complete_document(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind)
    config = fixture.config()
    config = replace(config, limits=replace(config.limits, max_content_bytes=8))
    async with _source(fixture, monkeypatch, config=config) as source:
        with pytest.raises(mcp.MCPSourceError, match="limit"):
            await source.read(APPROVED)
        assert len(fixture.calls) == 1


async def test_search_cursor_cannot_be_reused_for_another_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture("tools", partial=True)
    async with _source(fixture, monkeypatch) as source:
        page = await source.search("selected")
        before = len(fixture.requests)
        with pytest.raises(mcp.MCPSourceError, match="denied"):
            await source.search("different query", page.next_cursor)
        assert len(fixture.requests) == before


async def test_mapped_citation_does_not_change_requested_identity_or_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture("tools", alternate_citation=True)
    async with _source(fixture, monkeypatch) as source:
        evidence = await source.read(APPROVED)
        assert evidence.resource_id == APPROVED
        assert evidence.uri == "https://citation.example/selected"
        assert evidence.receipt is not None
        assert evidence.receipt.requested_id == APPROVED
        assert fixture.calls == [("read_document", {"id": APPROVED, "tenant": "selected-tenant"})]
        reads = [request for request in fixture.requests if request.get("method") == "tools/call"]
        assert len(reads) == 1
        assert reads[0]["params"]["arguments"]["id"] == APPROVED


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_read_receipt_changes_when_approved_scope_changes(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipts = []
    for selected_ids in ((APPROVED,), (APPROVED, EXTRA)):
        fixture = _Fixture(kind)
        config = replace(fixture.config(), approved_resource_ids=selected_ids)
        async with _source(fixture, monkeypatch, config=config) as source:
            evidence = await source.read(APPROVED)
            assert evidence.receipt is not None
            receipts.append(evidence.receipt)
    assert receipts[0].content_sha256 == receipts[1].content_sha256
    assert receipts[0].config_sha256 != receipts[1].config_sha256


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_invented_cursor_is_refused_before_upstream_request(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind)
    async with _source(fixture, monkeypatch) as source:
        before = len(fixture.requests)
        with pytest.raises(mcp.MCPSourceError, match="denied"):
            if kind == "resources":
                await source.list_resources("invented-cursor")
            else:
                await source.search("selected", "invented-cursor")
        assert len(fixture.requests) == before


@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_official_sdk_sse_response_frames_produce_read_receipt(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(kind, json_response=False)
    async with _source(fixture, monkeypatch) as source:
        evidence = await source.read(APPROVED)
        assert evidence.text == "Complete selected document."
        assert evidence.receipt is not None
        assert evidence.receipt.requested_id == APPROVED
        assert evidence.receipt.content_sha256 == hashlib.sha256(evidence.text.encode()).hexdigest()
