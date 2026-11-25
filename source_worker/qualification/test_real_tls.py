"""The installed worker and official MCP SDK over an owned loopback TLS listener."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import certifi
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from loom_source_worker import mcp_client
from loom_source_worker.credential_store import CredentialRecord
from loom_source_worker.direct_backend import ApprovedProfile, DirectBackend
from loom_source_worker.mcp_client import (
    MCPBearer,
    MCPSourceClient,
    MCPSourceConfig,
    MCPSourceError,
    MCPToolMapping,
)
from loom_source_worker.protocol import ConnectionBinding, ReadBinding, ReadGrant, issue_read_grant
from loom_source_worker.read_service import ReadFailure, ReadLimits, ReadService
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyUrl, SecretStr
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("LOOM_SOURCE_WORKER_REAL_TLS") != "1"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted",
        reason="requires the installed-worker GitHub-hosted TLS step",
    ),
]

_HOST = "mcp-worker.invalid"
_WRONG_HOST = "other-worker.invalid"
_LOOPBACK = "127.0.0.1"
_ACCOUNTS = ("alice", "bob")
_REFUSALS = {"expired": 401, "forbidden": 403}


def _schema(*fields: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in fields},
        "required": list(fields),
        "additionalProperties": False,
    }


def _identity(account: str) -> str:
    return f"urn:fixture:tls:{account}"


def _text(mode: str, account: str) -> str:
    return f"{mode}: synthetic content for {account}"


def _sdk_server(mode: str, account: str) -> StreamableHTTPSessionManager:
    sdk: Server[Any, Any] = Server(f"fixture-{mode}-{account}")
    identity = _identity(account)
    if mode == "resource":

        @sdk.list_resources()
        async def list_resources() -> list[types.Resource]:
            return [types.Resource(uri=AnyUrl(identity), name=account)]

        @sdk.read_resource()
        async def read_resource(uri: AnyUrl) -> str:
            assert str(uri) == identity
            return _text(mode, account)

    else:

        @sdk.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name="read",
                    inputSchema=_schema("id"),
                    outputSchema=_schema("id", "title", "uri", "text"),
                )
            ]

        @sdk.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, str]:
            assert name == "read" and arguments == {"id": identity}
            return {"id": identity, "title": account, "uri": identity, "text": _text(mode, account)}

    return StreamableHTTPSessionManager(app=sdk, json_response=mode == "mapped", stateless=False)


def _certificates(tmp_path: Path) -> tuple[Path, Path, bytes]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Worker fixture root")])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _HOST)])
    now = datetime.now(UTC)

    def builder(name: x509.Name, key: ec.EllipticCurvePrivateKey) -> x509.CertificateBuilder:
        return (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(hours=1))
        )

    ca = (
        builder(ca_name, ca_key)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        builder(leaf_name, leaf_key)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(_HOST)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "leaf.pem", tmp_path / "leaf.key"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as key:
        key.write(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path, ca.public_bytes(serialization.Encoding.PEM)


@contextmanager
def _trust_fixture_ca(
    tmp_path: Path, root: bytes, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    original = ssl.SSLContext.load_verify_locations
    bundle = Path(certifi.where())
    combined = tmp_path / "fixture-ca-bundle.pem"
    combined.write_bytes(bundle.read_bytes() + b"\n" + root)

    def load_locations(
        context: ssl.SSLContext,
        cafile: str | os.PathLike[str] | None = None,
        capath: str | os.PathLike[str] | None = None,
        cadata: str | bytes | None = None,
    ) -> None:
        if cafile is not None and Path(cafile) == bundle:
            cafile = str(combined)
        original(context, cafile=cafile, capath=capath, cadata=cadata)

    with monkeypatch.context() as scoped:
        scoped.setattr(ssl.SSLContext, "load_verify_locations", load_locations)
        yield


class _FixtureServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@dataclass
class _Fixture:
    port: int
    root: bytes
    sni: list[str | None] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)
    sessions: dict[str, str] = field(default_factory=dict)
    terminations: list[tuple[str, str, int]] = field(default_factory=list)
    refusals: list[tuple[str, int]] = field(default_factory=list)
    stalled: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)

    def endpoint(self, host: str = _HOST) -> str:
        return f"https://{host}:{self.port}/mcp"


@asynccontextmanager
async def _service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "resource"
) -> AsyncIterator[_Fixture]:
    cert, key, root = _certificates(tmp_path)
    managers = {account: _sdk_server(mode, account) for account in _ACCOUNTS}
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
        monkeypatch.context() as scoped,
    ):
        listener.bind((_LOOPBACK, 0))
        fixture = _Fixture(listener.getsockname()[1], root)

        async def fixture_pin(host: str) -> ipaddress.IPv4Address:
            assert host in {_HOST, _WRONG_HOST}
            return ipaddress.IPv4Address(_LOOPBACK)

        scoped.setattr(mcp_client, "resolve_public_address", fixture_pin)

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            request = Request(scope, receive)
            assert request.url.scheme == "https" and request.url.path == "/mcp"
            assert scope["server"] == (_LOOPBACK, fixture.port)
            assert request.headers["host"] == f"{_HOST}:{fixture.port}"
            bearer = request.headers["authorization"]
            prefix = "Bearer fixture-"
            assert bearer.startswith(prefix)
            account = bearer.removeprefix(prefix)
            fixture.requests.append((account, request.method))
            if account in _REFUSALS:
                status = _REFUSALS[account]
                fixture.refusals.append((account, status))
                await Response(status_code=status)(scope, receive, send)
                return
            if account == "stall":
                assert request.method == "POST"
                assert json.loads(await request.body())["method"] == "initialize"
                fixture.stalled.set()
                assert (await receive())["type"] == "http.disconnect"
                fixture.disconnected.set()
                return

            async def observe(message: Message) -> None:
                if message["type"] == "http.response.start":
                    session = request.headers.get("mcp-session-id")
                    if request.method == "DELETE":
                        assert session
                        fixture.terminations.append((account, session, message["status"]))
                    elif request.method == "POST" and session is None:
                        assigned = dict(message["headers"]).get(b"mcp-session-id")
                        if assigned:
                            fixture.sessions[account] = assigned.decode("ascii")
                await send(message)

            await managers[account].handle_request(scope, receive, observe)

        config = uvicorn.Config(
            app,
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
            http="h11",
            lifespan="off",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            timeout_graceful_shutdown=3,
        )
        config.load()
        assert config.ssl is not None

        def record_sni(_connection: Any, name: str | None, _context: ssl.SSLContext) -> None:
            fixture.sni.append(name)

        config.ssl.set_servername_callback(record_sni)
        server = _FixtureServer(config)
        async with AsyncExitStack() as stack:
            for manager in managers.values():
                await stack.enter_async_context(manager.run())
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if task.done():
                            task.result()
                            pytest.fail("TLS listener exited before startup")
                        await asyncio.sleep(0.01)
                yield fixture
            finally:
                server.should_exit = True
                async with asyncio.timeout(5):
                    await task


@dataclass
class _Accounts:
    records: dict[UUID, CredentialRecord] = field(default_factory=dict)

    async def get(self, connection_id: UUID) -> CredentialRecord:
        return self.records[connection_id]


def _bind_account(
    worker_id: UUID, profile: ApprovedProfile, accounts: _Accounts, account: str
) -> ReadBinding:
    binding = ReadBinding(
        worker_id=worker_id,
        org="fixture-org",
        namespace="fixture-team",
        source_id=uuid4(),
        selection_generation=1,
        requester=account,
        connection_id=uuid4(),
        connection_generation=1,
        profile_digest=profile.digest,
        resource_id=_identity(account),
    )
    accounts.records[binding.connection_id] = CredentialRecord(
        binding=ConnectionBinding(
            worker_id=worker_id,
            org=binding.org,
            owner=account,
            connection_id=binding.connection_id,
            generation=1,
            profile_digest=profile.digest,
        ),
        state="active",
        auth="bearer",
        audience=profile.config.bearer_audience,
        token=SecretStr(f"fixture-{account}"),
        expires_at=int(time.time()) + 60,
    )
    return binding


@pytest.mark.parametrize("mode", ["resource", "mapped"])
async def test_signed_exact_reads_over_tls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    async with _service(tmp_path, monkeypatch, mode) as fixture:
        with _trust_fixture_ca(tmp_path, fixture.root, monkeypatch):
            endpoint = fixture.endpoint()
            mapping = (
                MCPToolMapping("read", _schema("id"), _schema("id", "title", "uri", "text"), "id")
                if mode == "mapped"
                else None
            )
            profile = ApprovedProfile(
                MCPSourceConfig(
                    endpoint,
                    bearer_audience=endpoint,
                    approved_tools=("read",) if mapping else (),
                    read=mapping,
                ),
                ("fixture-org",),
            )
            worker_id = uuid4()
            accounts = _Accounts()
            bindings = [_bind_account(worker_id, profile, accounts, name) for name in _ACCOUNTS]
            key = Ed25519PrivateKey.generate()
            service = ReadService(
                worker_id=worker_id,
                issuer="fixture-issuer",
                verifier=key.public_key(),
                backend=DirectBackend(accounts, (profile,)),
                limits=ReadLimits(
                    concurrent=2, concurrent_per_org=2, timeout_seconds=10, content_bytes=2048
                ),
            )

            def sign(binding: ReadBinding) -> tuple[str, UUID]:
                grant = ReadGrant(request_id=uuid4(), operation="live", binding=binding)
                token = issue_read_grant(
                    grant, issuer="fixture-issuer", key=key, lifetime_seconds=20
                )
                return token, grant.request_id

            wrong_owner = bindings[0].model_copy(update={"requester": "bob"})
            token, _ = sign(wrong_owner)
            with pytest.raises(ReadFailure, match="^denied$"):
                await service.execute(token, "live")
            assert not fixture.requests

            for binding in bindings:
                token, request_id = sign(binding)
                result = await service.execute(token, "live")
                assert result.request_id == request_id
                assert result.document is not None
                assert result.document.resource_id == binding.resource_id
                expected = _text(mode, binding.requester)
                assert result.document.text == expected
                assert result.content_sha256 == hashlib.sha256(expected.encode()).hexdigest()

        assert set(fixture.sessions) == set(_ACCOUNTS)
        assert len(set(fixture.sessions.values())) == len(_ACCOUNTS)
        assert fixture.terminations == [(name, fixture.sessions[name], 200) for name in _ACCOUNTS]
        assert fixture.sni and set(fixture.sni) == {_HOST}


async def _current() -> None:
    return None


def _client(endpoint: str, account: str, *, deadline: float | None = None) -> MCPSourceClient:
    return MCPSourceClient(
        MCPSourceConfig(endpoint, bearer_audience=endpoint),
        MCPBearer(f"fixture-{account}", endpoint),
        validate_access=_current,
        deadline=deadline,
    )


async def test_ca_and_hostname_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _service(tmp_path, monkeypatch) as fixture:
        with pytest.raises(MCPSourceError, match="^MCP source unavailable$"):
            async with _client(fixture.endpoint(), "alice"):
                pytest.fail("untrusted fixture CA was accepted")
        assert fixture.sni == [_HOST]
        assert not fixture.requests
        with _trust_fixture_ca(tmp_path, fixture.root, monkeypatch):
            with pytest.raises(MCPSourceError, match="^MCP source unavailable$"):
                async with _client(fixture.endpoint(_WRONG_HOST), "alice"):
                    pytest.fail("certificate for a different hostname was accepted")
            assert fixture.sni == [_HOST, _WRONG_HOST]
            assert not fixture.requests
            async with _client(fixture.endpoint(), "alice"):
                assert fixture.sessions["alice"]
        assert fixture.terminations == [("alice", fixture.sessions["alice"], 200)]


@pytest.mark.parametrize(
    ("account", "status", "code"), [("expired", 401, "reconnect"), ("forbidden", 403, "denied")]
)
async def test_tls_http_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: str, status: int, code: str
) -> None:
    async with _service(tmp_path, monkeypatch) as fixture:
        with _trust_fixture_ca(tmp_path, fixture.root, monkeypatch):
            with pytest.raises(MCPSourceError, match=f"^MCP source {code}$"):
                async with _client(fixture.endpoint(), account):
                    pytest.fail("HTTP refusal opened a session")
        assert fixture.refusals == [(account, status)]
        assert fixture.requests == [(account, "POST")]
        assert not fixture.sessions


async def test_deadline_closes_tls_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _service(tmp_path, monkeypatch) as fixture:
        with _trust_fixture_ca(tmp_path, fixture.root, monkeypatch):
            deadline = asyncio.get_running_loop().time() + 4
            async with asyncio.timeout(12):
                with pytest.raises(MCPSourceError, match="^MCP source timeout$"):
                    async with _client(fixture.endpoint(), "stall", deadline=deadline):
                        pytest.fail("stalled initialize opened a session")
                assert fixture.stalled.is_set()
                await fixture.disconnected.wait()
        assert fixture.requests == [("stall", "POST")]
        assert not fixture.sessions
