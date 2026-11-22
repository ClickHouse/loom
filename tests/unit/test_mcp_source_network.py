"""An owned TLS server checks the protocol client's actual HTTPS and SSE boundary."""

from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from server.clients import mcp_source as source

RESOURCE = "document://selected/one"
BODY = "Selected synthetic text delivered over TLS and SSE."
TOKEN = "synthetic-source-token"


def _certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "source.example")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("source.example")]), False)
        .sign(key, hashes.SHA256())
    )
    certificate, private_key = directory / "certificate.pem", directory / "key.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate, private_key


class _OwnedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The test supervisor owns signals; this temporary listener owns only its task.
        yield


@asynccontextmanager
async def _serve(app: Any, certificate: Path, key: Path) -> AsyncIterator[int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = _OwnedServer(
        uvicorn.Config(
            app,
            log_config=None,
            log_level="critical",
            access_log=False,
            ssl_certfile=str(certificate),
            ssl_keyfile=str(key),
            lifespan="on",
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("owned server stopped before startup")
                await asyncio.sleep(0)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(5):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
async def test_official_sdk_reads_over_verified_tls_and_sse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, oversized: bool
) -> None:
    certificate, key = _certificate(tmp_path)
    sdk = FastMCP(
        "Synthetic TLS source",
        stateless_http=True,
        json_response=False,
        transport_security=TransportSecuritySettings(allowed_hosts=["source.example:*"]),
    )

    @sdk.resource(RESOURCE, name="Selected document", mime_type="text/plain")
    async def resource() -> str:
        return "x" * 32_768 if oversized else BODY

    app = sdk.streamable_http_app()
    requests: list[dict[bytes, bytes]] = []
    content_types: list[bytes] = []

    async def observed(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            requests.append(dict(scope["headers"]))

        async def response(message: Any) -> None:
            if message["type"] == "http.response.start":
                content_types.append(dict(message["headers"]).get(b"content-type", b""))
            await send(message)

        await app(scope, receive, response)

    async with _serve(observed, certificate, key) as port:
        endpoint = f"https://source.example:{port}/mcp"

        async def pin(url: str, *, schemes: Any) -> tuple[str, str, str, str]:
            assert url == endpoint
            assert schemes == ("https",)
            return "", f"https://127.0.0.1:{port}/mcp", f"source.example:{port}", "source.example"

        async def authority() -> None:
            return None

        monkeypatch.setattr(source, "pinned_target", pin)
        context = ssl.create_default_context(cafile=str(certificate))
        config = source.MCPSourceConfig(
            endpoint,
            bearer_audience=endpoint,
            approved_resource_ids=(RESOURCE,),
            limits=source.MCPLimits(timeout_seconds=5, request_seconds=2, max_wire_bytes=4096),
        )
        async with source.MCPSourceClient(
            config,
            source.MCPBearer(TOKEN, endpoint),
            validate_access=authority,
            _transport=httpx.AsyncHTTPTransport(verify=context),
        ) as client:
            page = await client.list_resources()
            assert page.items[0].receipt is None
            if oversized:
                with pytest.raises(source.MCPSourceError) as refusal:
                    await client.read(RESOURCE)
                assert refusal.value.code == "limit"
            else:
                evidence = await client.read(RESOURCE)
                assert evidence.text == BODY
                assert evidence.receipt is not None
                assert evidence.receipt.content_sha256 == hashlib.sha256(BODY.encode()).hexdigest()
        assert requests
        assert all(row[b"host"] == f"source.example:{port}".encode() for row in requests)
        assert all(row[b"authorization"] == f"Bearer {TOKEN}".encode() for row in requests)
        assert any(value.startswith(b"text/event-stream") for value in content_types)
