"""Official SDK fixtures for a fixed source workload; all transport is ASGI."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from tests.integration.mcp_source_qualification_servers import (
    DOCUMENT,
    READ_INPUT,
    SELECTED,
    SyntheticSource,
)

SCALE_DOCUMENT = copy.deepcopy(DOCUMENT)
SCALE_DOCUMENT["properties"]["text"]["maxLength"] = 8192
ROLES = ("healthy", "stalled", "rate_limited", "denied", "malformed")


@dataclass
class ScaleSource(SyntheticSource):
    role: str = "healthy"
    faults_enabled: bool = False
    hold: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.text = "Synthetic selected text.\n" + "x" * (8192 - len("Synthetic selected text.\n"))
        assert len(self.text.encode()) == 8192

    @property
    def host(self) -> str:
        return self.kind + "-" + self.role.replace("_", "-") + ".mcp-scale.example"

    def profile(self, orgs: list[str], *, copy_allowed: bool = True) -> dict[str, Any]:
        value = super().profile(orgs, copy_allowed=copy_allowed)
        value["id"] = "scale-" + self.kind + "-" + self.role
        if self.kind == "tools":
            value["config"]["approved_tools"] = ["read_document"]
            value["config"]["read"]["output_schema"] = copy.deepcopy(SCALE_DOCUMENT)
            del value["config"]["search"]
        return value

    async def read(self, identity: str) -> str:
        assert identity == SELECTED
        self.reads.append(identity)
        self.in_flight += 1
        self.started.set()
        try:
            if self.hold or self.faults_enabled and self.role == "stalled":
                await self.release.wait()
            else:
                await asyncio.sleep(0.02)
            return self.text
        finally:
            self.in_flight -= 1

    def tool_server(self) -> Server:
        server = Server("Scale mapped tools")

        @server.list_tools()
        async def listing() -> list[types.Tool]:
            return [
                types.Tool(
                    name="read_document", inputSchema=READ_INPUT, outputSchema=SCALE_DOCUMENT
                )
            ]

        @server.call_tool()
        async def call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            assert name == "read_document"
            identity = arguments["id"]
            result = {
                "id": identity,
                "uri": identity,
                "title": "Synthetic selected text",
                "text": await self.read(identity),
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
            )

        return server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.faults_enabled and self.role in {"rate_limited", "denied", "malformed"}:
            code = {"rate_limited": 429, "denied": 403, "malformed": 200}[self.role]
            await JSONResponse(
                {"fixture": "unavailable"},
                status_code=code,
                headers={"Retry-After": "1"} if code == 429 else {},
            )(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
