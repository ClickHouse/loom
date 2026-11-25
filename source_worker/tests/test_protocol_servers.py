"""Installed worker against owned official SDK servers over ASGI HTTP."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, replace
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
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

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


async def _synthetic_pin(host: str) -> ipaddress.IPv6Address:
    assert host == "source.example.test"
    return ipaddress.IPv6Address("2600::10")


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

    monkeypatch.setattr(mcp_client, "resolve_public_address", _synthetic_pin)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["resource", "tool"])
@pytest.mark.parametrize("phase", ["initialize", "read"])
@pytest.mark.parametrize(("status", "code"), [(401, "reconnect"), (403, "denied")])
async def test_upstream_http_refusal_at_sdk_boundary(
    mode: str, phase: str, status: int, code: mcp_client.ErrorCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = 0

    def on_read() -> None:
        nonlocal reads
        reads += 1

    mapped = mode == "tool"
    manager = _tool_server(on_read) if mapped else _resource_server(on_read)
    config = MCPSourceConfig(
        endpoint=_ENDPOINT,
        bearer_audience=_ENDPOINT,
        approved_tools=("read",) if mapped else (),
        read=MCPToolMapping("read", _INPUT, _OUTPUT, "id") if mapped else None,
        limits=MCPLimits(timeout_seconds=12, request_seconds=8),
    )
    read_method = "tools/call" if mapped else "resources/read"
    refusal_method = "initialize" if phase == "initialize" else read_method
    alice_header = b"Bearer synthetic-alice"
    armed = phase == "initialize"
    posts: list[tuple[bytes | None, str]] = []
    refusals: list[int] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] != "POST":
            await manager.handle_request(scope, receive, send)
            return

        body = await Request(scope, receive).body()
        method = json.loads(body)["method"]
        bearer = dict(scope["headers"]).get(b"authorization")
        posts.append((bearer, method))
        if armed and bearer == alice_header and method == refusal_method:
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b"synthetic private refusal"})
            refusals.append(status)
            return

        replay_body: bytes | None = body

        async def replay() -> Message:
            nonlocal replay_body
            if replay_body is None:
                return await receive()
            payload, replay_body = replay_body, None
            return {"type": "http.request", "body": payload}

        await manager.handle_request(scope, replay, send)

    async def validate() -> None:
        return None

    def client_for(token: str, selected: str) -> mcp_client.MCPSourceClient:
        return mcp_client.MCPSourceClient(
            replace(config, approved_resource_ids=(selected,)),
            mcp_client.MCPBearer(token, _ENDPOINT),
            validate_access=validate,
            _transport=_FragmentedASGI(app=app),
        )

    monkeypatch.setattr(mcp_client, "resolve_public_address", _synthetic_pin)
    alice = client_for("synthetic-alice", _DOC)
    async with manager.run():
        if phase == "initialize":
            with pytest.raises(mcp_client.MCPSourceError, match=f"^MCP source {code}$") as refusal:
                async with alice:
                    pytest.fail("upstream refusal opened a client session")
            assert refusal.value.code == code
            assert posts == [(alice_header, "initialize")]
            assert reads == 0
        else:
            async with client_for("synthetic-bob", "urn:fixture:peer") as peer:
                async with alice:
                    evidence = await alice.read(_DOC)
                    assert evidence.resource_id == _DOC
                    assert evidence.text == f"{mode} fixture body"
                    assert evidence.receipt is not None
                    assert evidence.receipt.operation == read_method
                    assert evidence.receipt.requested_id == _DOC
                    assert evidence.receipt.identity_verified
                    armed = True
                    # A received refusal must finish before the SDK request deadline.
                    async with asyncio.timeout(2):
                        with pytest.raises(
                            mcp_client.MCPSourceError, match=f"^MCP source {code}$"
                        ) as refusal:
                            await alice.read(_DOC)
                    assert refusal.value.code == code

                if mapped:
                    assert (await peer.discover()).tools == ("read",)
                else:
                    [listed] = (await peer.list_resources()).items
                    assert listed.resource_id == _DOC
                with pytest.raises(mcp_client.MCPSourceError, match="^MCP source denied$"):
                    await peer.read(_DOC)

            read_bearers = [bearer for bearer, method in posts if method == read_method]
            assert read_bearers == [alice_header, alice_header]
            assert reads == 1
    assert refusals == [status]


@pytest.mark.asyncio
async def test_cancel_terminates_an_active_stateful_sdk_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    terminations: list[tuple[bytes, int]] = []
    server: Server[Any, Any] = Server("synthetic-stateful-resources")

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [types.Resource(uri=AnyUrl(_DOC), name="Resource")]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        assert str(uri) == _DOC
        started.set()
        return await asyncio.Future[str]()

    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=False)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async def watch(message: Any) -> None:
            if scope["method"] == "DELETE" and message["type"] == "http.response.start":
                terminations.append((dict(scope["headers"])[b"mcp-session-id"], message["status"]))
            await send(message)

        await manager.handle_request(scope, receive, watch)

    async def validate() -> None:
        return None

    monkeypatch.setattr(mcp_client, "resolve_public_address", _synthetic_pin)
    config = MCPSourceConfig(endpoint=_ENDPOINT, approved_resource_ids=(_DOC,))
    async with (
        asyncio.timeout(5),
        manager.run(),
        mcp_client.MCPSourceClient(
            config, None, validate_access=validate, _transport=httpx.ASGITransport(app=app)
        ) as client,
    ):
        read = asyncio.create_task(client.read(_DOC))
        await started.wait()
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
    assert len(terminations) == 1 and terminations[0][0] and terminations[0][1] == 200
