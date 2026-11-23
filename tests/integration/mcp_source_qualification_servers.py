"""Owned SDK protocol fixtures; no public endpoint or provider is contacted."""

from __future__ import annotations

import asyncio
import copy
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

SELECTED = "qualification://documents/selected"
SECOND = "qualification://documents/second"
UNSELECTED = "qualification://documents/unselected"


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


STRING = {"type": "string", "maxLength": 4096}
DOCUMENT = object_schema(
    {key: STRING for key in ("id", "title", "text", "uri")}, ["id", "title", "text", "uri"]
)
READ_INPUT = object_schema({"id": STRING}, ["id"])
SEARCH_INPUT = object_schema({"query": STRING, "cursor": STRING}, ["query"])
SEARCH_OUTPUT = object_schema(
    {"items": {"type": "array", "items": DOCUMENT, "maxItems": 3}, "next_cursor": STRING},
    ["items"],
)


@dataclass
class SyntheticSource:
    kind: str
    json_response: bool = True
    bearer: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)
    marker: str = field(default_factory=lambda: "qualificationcontent" + secrets.token_hex(12))
    text: str = ""
    denied: bool = False
    drift: bool = False
    partial: bool = False
    block_read: bool = False
    reads: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    in_flight: int = 0
    app: ASGIApp | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.text = "# Selected document\n\n" + self.marker + " café 日本語.\n"

    @property
    def host(self) -> str:
        return self.kind + ".mcp-qualification.example"

    @property
    def endpoint(self) -> str:
        return "https://" + self.host + "/mcp"

    async def read(self, identity: str) -> str:
        assert identity in {SELECTED, SECOND}, "Unselected content must never be read"
        self.reads.append(identity)
        self.in_flight += 1
        self.started.set()
        try:
            if self.block_read:
                await self.release.wait()
            return self.text if identity == SELECTED else "Second selected document."
        finally:
            self.in_flight -= 1

    def profile(self, orgs: list[str], *, copy_allowed: bool = True) -> dict[str, Any]:
        config: dict[str, Any] = {
            "endpoint": self.endpoint,
            "bearer_audience": self.endpoint,
            "limits": {"timeout_seconds": 20, "request_seconds": 10, "max_pages": 2},
        }
        if self.kind == "tools":
            config.update(
                approved_tools=["search_documents", "read_document"],
                read={
                    "name": "read_document",
                    "input_schema": READ_INPUT,
                    "output_schema": DOCUMENT,
                    "argument_name": "id",
                },
                search={
                    "name": "search_documents",
                    "input_schema": SEARCH_INPUT,
                    "output_schema": SEARCH_OUTPUT,
                    "argument_name": "query",
                    "items_path": ["items"],
                    "cursor_argument": "cursor",
                    "next_cursor_path": ["next_cursor"],
                },
            )
        return {
            "id": "qualification-" + self.kind,
            "label": "Synthetic MCP source",
            "auth": "bearer",
            "allowed_orgs": orgs,
            "allow_collection_copy": copy_allowed,
            "config": config,
        }

    def resource_server(self) -> FastMCP:
        server = FastMCP(
            "Qualification resources",
            stateless_http=True,
            json_response=self.json_response,
            transport_security=TransportSecuritySettings(allowed_hosts=[self.host]),
        )

        @server.resource(SELECTED, name="Selected synthetic document")
        async def selected() -> str:
            return await self.read(SELECTED)

        @server.resource(SECOND, name="Second synthetic document")
        async def second() -> str:
            return await self.read(SECOND)

        @server.resource(UNSELECTED, name="Unselected synthetic document")
        async def unselected() -> str:
            raise AssertionError("Unselected resource was read")

        original = server._mcp_server.request_handlers[types.ListResourcesRequest]

        async def listing(request: types.ListResourcesRequest) -> types.ServerResult:
            self.calls.append("resources/list")
            result = await original(request)
            if self.partial:
                assert isinstance(result.root, types.ListResourcesResult)
                result.root.resources = result.root.resources[:1]
                result.root.nextCursor = "unfinished"
            return result

        server._mcp_server.request_handlers[types.ListResourcesRequest] = listing

        return server

    def tool_server(self) -> Server:
        # The low-level SDK server exercises a separate dispatch implementation.
        server = Server("Qualification mapped tools")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            schema = copy.deepcopy(READ_INPUT)
            if self.drift:
                schema["properties"]["id"]["maxLength"] = 64
            return [
                types.Tool(name="read_document", inputSchema=schema, outputSchema=DOCUMENT),
                types.Tool(
                    name="search_documents", inputSchema=SEARCH_INPUT, outputSchema=SEARCH_OUTPUT
                ),
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            self.calls.append(name)
            if name == "read_document":
                identity = arguments["id"]
                text = await self.read(identity)
                result: dict[str, Any] = {
                    "id": identity,
                    "uri": identity,
                    "title": "Selected synthetic document",
                    "text": text,
                }
            else:
                assert name == "search_documents"
                result = {
                    "items": [
                        {
                            "id": identity,
                            "uri": identity,
                            "title": "Search result",
                            "text": "Search snippet only",
                        }
                        for identity in (SELECTED, UNSELECTED)
                    ],
                }
                if self.partial:
                    result["next_cursor"] = "unfinished"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
            )

        return server

    @asynccontextmanager
    async def application(self) -> AsyncIterator[None]:
        if self.kind == "resources":
            app = self.resource_server().streamable_http_app()
            async with app.router.lifespan_context(app):
                self.app = app
                try:
                    yield
                finally:
                    self.release.set()
                    self.app = None
        else:
            manager = StreamableHTTPSessionManager(
                app=self.tool_server(),
                json_response=self.json_response,
                stateless=True,
                security_settings=TransportSecuritySettings(allowed_hosts=[self.host]),
            )

            class Endpoint:
                async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                    await manager.handle_request(scope, receive, send)

            async with manager.run():
                self.app = Starlette(
                    routes=[Route("/mcp", Endpoint(), methods=["POST", "GET", "DELETE"])]
                )
                try:
                    yield
                finally:
                    self.release.set()
                    self.app = None
        assert self.in_flight == 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert self.app is not None
        assert scope["path"] == "/mcp"
        headers = dict(scope["headers"])
        assert headers[b"host"] == self.host.encode()
        if self.denied or headers.get(b"authorization") != ("Bearer " + self.bearer).encode():
            await JSONResponse({"error": "Synthetic refusal"}, status_code=403)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


class SourceTransport(httpx.AsyncBaseTransport):
    """Only owned canonical fixture names can reach the in-process SDK servers."""

    def __init__(self, sources: list[SyntheticSource]) -> None:
        self.sources = {source.host: source for source in sources}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # The real pinned transport changes the connect address, retaining Host/SNI.
        source = self.sources[request.headers["host"]]
        assert request.url.host == "203.0.113.8"
        assert request.extensions["sni_hostname"] in {source.host, source.host.encode()}
        return await httpx.ASGITransport(app=source).handle_async_request(request)
