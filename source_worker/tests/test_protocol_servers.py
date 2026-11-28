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

import dns.asyncresolver
import dns.rdata
import dns.resolver
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from loom_source_worker import direct_backend, mcp_client
from loom_source_worker.credential_store import CredentialRecord
from loom_source_worker.direct_backend import ApprovedProfile, DirectBackend
from loom_source_worker.http_service import create_app
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


def _resource_server(
    on_read: Callable[[], None], *, stateless: bool = True
) -> StreamableHTTPSessionManager:
    server: Server[Any, Any] = Server("synthetic-resources")

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [types.Resource(uri=AnyUrl(_DOC), name="Resource")]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        assert str(uri) == _DOC
        on_read()
        return "resource fixture body"

    return StreamableHTTPSessionManager(
        app=server, json_response=not stateless, stateless=stateless
    )


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
@pytest.mark.parametrize(
    ("mode", "phase"),
    [
        ("resource", "initialize"),
        ("resource", "read"),
        ("tool", "initialize"),
        ("tool", "read"),
        ("tool", "discover"),
    ],
)
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
    refusal_method = {"initialize": "initialize", "read": read_method, "discover": "tools/list"}[
        phase
    ]
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
            assert read_bearers == [alice_header] * (1 if phase == "discover" else 2)
            if phase == "discover":
                assert [method for bearer, method in posts if bearer == alice_header][-1] == (
                    "tools/list"
                )
            assert reads == 1
    assert refusals == [status]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "operation"), [("live", "live"), ("check-access", "check_access")]
)
@pytest.mark.parametrize(
    ("fault", "status", "code"),
    [
        ("network", 503, "unavailable"),
        ("upstream-503", 503, "unavailable"),
        ("expired-session", 503, "unavailable"),
        ("missing-endpoint", 422, "unsupported"),
        ("dns-unavailable", 503, "unavailable"),
        ("dns-timeout", 504, "timeout"),
        ("dns-prohibited", 403, "denied"),
    ],
)
async def test_signed_route_distinguishes_transport_failure_from_unsupported(
    route: str,
    operation: str,
    fault: str,
    status: int,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faulted = True
    stale_session: bytes | None = None
    successful_sessions: list[bytes] = []
    upstream_requests: list[str] = []
    queries: list[str] = []
    manager = _resource_server(lambda: None, stateless=False)

    class Resolver:
        async def resolve(self, host: str, family: str, **options: Any) -> list[dns.rdata.Rdata]:
            assert host == "source.example.test"
            assert options == {"search": False, "raise_on_no_answer": False}
            queries.append(family)
            if family == ("A" if route == "live" else "AAAA"):
                address = "8.8.8.8" if family == "A" else "2600::10"
            elif faulted and fault == "dns-unavailable":
                raise dns.resolver.NoNameservers("synthetic private DNS detail")
            elif faulted and fault == "dns-timeout":
                raise dns.resolver.LifetimeTimeout(timeout=0.1, errors=[])
            elif faulted and fault == "dns-prohibited":
                address = "127.0.0.1" if family == "A" else "::1"
            else:
                return []
            return [dns.rdata.from_text("IN", family, address)]

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal stale_session
        upstream_requests.append(scope["method"])
        if scope["method"] != "POST":
            await manager.handle_request(scope, receive, send)
            return
        body = await Request(scope, receive).body()
        method = json.loads(body)["method"]
        session = dict(scope["headers"]).get(b"mcp-session-id")
        if method == "initialize" and faulted and fault == "network":
            raise httpx.ConnectError("synthetic private network detail")
        refused = None
        if method == "initialize" and faulted and fault in {"upstream-503", "missing-endpoint"}:
            assert session is None
            refused = 503 if fault == "upstream-503" else 404
        elif method == "resources/read":
            assert session
            if faulted and fault == "expired-session":
                stale_session = session
                refused = 404
            else:
                successful_sessions.append(session)
        if refused is not None:
            await send({"type": "http.response.start", "status": refused, "headers": []})
            await send({"type": "http.response.body", "body": b"synthetic private HTTP detail"})
            return

        replay_body: bytes | None = body

        async def replay() -> Message:
            nonlocal replay_body
            if replay_body is None:
                return await receive()
            payload, replay_body = replay_body, None
            return {"type": "http.request", "body": payload}

        await manager.handle_request(scope, replay, send)

    def routed_client(config, credential, *, validate_access, deadline):
        return mcp_client.MCPSourceClient(
            config,
            credential,
            validate_access=validate_access,
            deadline=deadline,
            _transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(direct_backend, "MCPSourceClient", routed_client)
    monkeypatch.setattr(dns.asyncresolver, "Resolver", Resolver)
    profile = ApprovedProfile(
        MCPSourceConfig(endpoint=_ENDPOINT, bearer_audience=_ENDPOINT), ("org",)
    )
    binding = ReadBinding(
        worker_id=uuid4(),
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
    key = Ed25519PrivateKey.generate()
    service = ReadService(
        worker_id=binding.worker_id,
        issuer="loom",
        verifier=key.public_key(),
        backend=DirectBackend(_SyntheticAccounts(binding), (profile,)),
        limits=ReadLimits(
            concurrent=4, concurrent_per_org=2, timeout_seconds=4, content_bytes=2048
        ),
    )

    def headers() -> dict[str, str]:
        grant = ReadGrant.model_validate(
            {"request_id": uuid4(), "operation": operation, "binding": binding}
        )
        return {
            "Authorization": "Bearer "
            + issue_read_grant(grant, issuer="loom", key=key, lifetime_seconds=10)
        }

    async with (
        manager.run(),
        httpx.AsyncClient(
            base_url="http://worker.test", transport=httpx.ASGITransport(app=create_app(service))
        ) as worker,
    ):
        failed = await worker.post(f"/v1/{route}", headers=headers())
        assert failed.status_code == status
        assert failed.json() == {"status": code}
        assert failed.headers["Cache-Control"] == "no-store"
        assert not successful_sessions
        assert sorted(queries) == ["A", "AAAA"]
        if fault.startswith("dns-"):
            assert upstream_requests == []
        faulted = False
        succeeded = await worker.post(f"/v1/{route}", headers=headers())
        assert succeeded.status_code == 200
        assert (succeeded.json()["document"] is not None) == (route == "live")
        assert len(successful_sessions) == 1 and successful_sessions[0] != stale_session


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_status", [200, 404])
async def test_cancel_terminates_an_active_stateful_sdk_session(
    delete_status: int,
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
        if scope["method"] == "DELETE" and delete_status == 404:
            terminations.append((dict(scope["headers"])[b"mcp-session-id"], 404))
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return

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
    assert len(terminations) == 1 and terminations[0][0] and terminations[0][1] == delete_status
