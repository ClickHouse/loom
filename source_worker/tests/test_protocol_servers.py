"""Installed worker against owned official SDK servers over ASGI HTTP."""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from loom_source_worker import direct_backend, mcp_client
from loom_source_worker.credential_store import CredentialRecord
from loom_source_worker.direct_backend import DirectBackend
from loom_source_worker.main import _parse_settings
from loom_source_worker.mcp_client import MCPLimits, MCPSourceConfig, MCPToolMapping
from loom_source_worker.protocol import ConnectionBinding, ReadBinding, ReadGrant, issue_read_grant
from loom_source_worker.read_service import ReadFailure, ReadLimits, ReadService
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyUrl, SecretStr
from starlette.types import Receive, Scope, Send

_ENDPOINT = "https://source.example.test/mcp"
_DOC = "urn:fixture:doc"
_INPUT = {
    "type": "object",
    "properties": {"id": {"type": "string"}},
    "required": ["id"],
    "additionalProperties": False,
}
_OUTPUT = {
    "type": "object",
    "properties": {key: {"type": "string"} for key in ("id", "title", "uri", "text")},
    "required": ["id", "title", "uri", "text"],
    "additionalProperties": False,
}


class _SyntheticAccounts:
    def __init__(self, binding: ReadBinding) -> None:
        self.records: dict[UUID, CredentialRecord] = {}
        self.reads = 0
        self.add(binding, "synthetic-alice")

    def add(self, binding: ReadBinding, token: str) -> None:
        self.records[binding.connection_id] = CredentialRecord(
            binding=ConnectionBinding(
                worker_id=binding.worker_id,
                org=binding.org,
                owner=binding.requester,
                connection_id=binding.connection_id,
                generation=binding.connection_generation,
                profile_digest=binding.profile_digest,
            ),
            auth="bearer",
            state="active",
            audience=_ENDPOINT,
            token=SecretStr(token),
            expires_at=int(time.time()) + 60,
        )

    async def get(self, connection_id: UUID) -> CredentialRecord:
        self.reads += 1
        return self.records[connection_id]


class _Fragments(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for body in self._stream:
            for offset in range(0, len(body), 4):
                yield body[offset : offset + 4]

    async def aclose(self) -> None:
        await self._stream.aclose()


class _FragmentedASGI(httpx.ASGITransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        return httpx.Response(
            response.status_code, headers=response.headers, stream=_Fragments(response.stream)
        )


def _resource_server(on_read: Callable[[], None]) -> StreamableHTTPSessionManager:
    server: Server[Any, Any] = Server("synthetic-resources")

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [types.Resource(uri=AnyUrl(_DOC), name="Resource")]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        assert str(uri) == _DOC
        on_read()
        return "resource fixture body"

    return StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)


def _tool_server(on_read: Callable[[], None]) -> StreamableHTTPSessionManager:
    server: Server[Any, Any] = Server("synthetic-mapped-tool")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name="read", inputSchema=_INPUT, outputSchema=_OUTPUT)]

    @server.call_tool()
    async def read_tool(name: str, arguments: dict[str, Any]) -> dict[str, str]:
        assert name == "read"
        assert arguments == {"id": _DOC}
        on_read()
        return {"id": _DOC, "title": "Mapped", "uri": _DOC, "text": "tool fixture body"}

    return StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["resource", "tool"])
async def test_read_and_proof_with_two_accounts(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    revoke: UUID | None = None

    def on_read() -> None:
        if revoke is not None:
            record = accounts.records[revoke]
            accounts.records[revoke] = record.model_copy(
                update={"binding": record.binding.model_copy(update={"generation": 2})}
            )

    if mode == "resource":
        manager = _resource_server(on_read)
        config = MCPSourceConfig(endpoint=_ENDPOINT, bearer_audience=_ENDPOINT)
    else:
        manager = _tool_server(on_read)
        config = MCPSourceConfig(
            endpoint=_ENDPOINT,
            bearer_audience=_ENDPOINT,
            approved_tools=("read",),
            limits=MCPLimits(max_requests=4),
            read=MCPToolMapping(
                name="read", input_schema=_INPUT, output_schema=_OUTPUT, argument_name="id"
            ),
        )
    allowed = {b"Bearer synthetic-alice"}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if dict(scope["headers"]).get(b"authorization") not in allowed:
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return

        await manager.handle_request(scope, receive, send)

    async def synthetic_pin(host: str) -> ipaddress.IPv6Address:
        assert host == "source.example.test"
        return ipaddress.IPv6Address("2600::10")

    def routed_client(
        config: Any, credential: Any, *, validate_access: Any, deadline: float
    ) -> Any:
        return mcp_client.MCPSourceClient(
            config,
            credential,
            validate_access=validate_access,
            deadline=deadline,
            _transport=_FragmentedASGI(app=app),
        )

    monkeypatch.setattr(mcp_client, "resolve_public_address", synthetic_pin)
    monkeypatch.setattr(direct_backend, "MCPSourceClient", routed_client)
    worker_id = uuid4()
    settings = _parse_settings(
        json.dumps(
            {
                "transport": "tls_proxy_unix",
                "worker_id": str(worker_id),
                "issuer": "loom",
                "vault": {"origin": "https://vault.example.test", "mount": "connections"},
                "read_limits": {
                    "concurrent": 4,
                    "concurrent_per_org": 2,
                    "timeout_seconds": 5,
                    "content_bytes": 2048,
                },
                "profiles": [
                    {
                        "config": {
                            key: value
                            for key, value in asdict(config).items()
                            if key != "approved_resource_ids"
                        },
                        "organizations": ["org"],
                    }
                ],
            }
        ).encode()
    )
    [profile] = settings.profiles
    binding = ReadBinding(
        worker_id=worker_id,
        org="org",
        namespace="team",
        source_id=uuid4(),
        selection_generation=1,
        requester="alice",
        connection_id=uuid4(),
        connection_generation=1,
        profile_digest=profile.digest,
        resource_id=_DOC,
    )
    bob = binding.model_copy(update={"requester": "bob", "connection_id": uuid4()})
    accounts = _SyntheticAccounts(binding)
    accounts.add(bob, "synthetic-bob")
    key = Ed25519PrivateKey.generate()
    service = ReadService(
        worker_id=binding.worker_id,
        issuer="loom",
        verifier=key.public_key(),
        backend=DirectBackend(accounts, (profile,)),
        limits=ReadLimits(
            concurrent=4, concurrent_per_org=2, timeout_seconds=5, content_bytes=2048
        ),
    )

    def grant(target: ReadBinding, operation: str) -> str:
        request = ReadGrant.model_validate(
            {"request_id": uuid4(), "operation": operation, "binding": target}
        )
        return issue_read_grant(request, issuer="loom", key=key, lifetime_seconds=10)

    async with manager.run():
        result = await service.execute(grant(binding, "live"), "live")
        assert result.document is not None
        assert result.document.text == f"{mode} fixture body"
        assert accounts.reads <= 32
        with pytest.raises(ReadFailure, match="^denied$"):
            await service.execute(grant(bob, "check_access"), "check_access")
        allowed.add(b"Bearer synthetic-bob")
        proof = await service.execute(grant(bob, "check_access"), "check_access")
        assert proof.document is None
        assert proof.content_sha256 == result.content_sha256
        revoke = bob.connection_id
        with pytest.raises(ReadFailure, match="^denied$"):
            await service.execute(grant(bob, "live"), "live")
