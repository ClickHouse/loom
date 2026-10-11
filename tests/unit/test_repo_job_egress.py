"""The egress proxy is the repository-job sandbox's only route out: CONNECT only,
to ports 443, 22 and the job's remote port, to hosts whose every address is
public, and always to the address it vetted (never the name again)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import server.core.hosts as hosts
from server.code.app.index import egress

PUBLIC = "93.184.215.14"
NAMES = {
    "public.example": [PUBLIC],
    "private.example": ["10.0.0.5"],
    "split.example": [PUBLIC, "192.168.1.7"],  # one private answer is enough to refuse
    "metadata.example": ["169.254.169.254"],
    "mapped.example": ["::ffff:127.0.0.1"],
}


@pytest.fixture(autouse=True)
def _resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str) -> list[str]:
        if host in NAMES:
            return NAMES[host]
        return [host]  # an address literal resolves to itself

    monkeypatch.setattr(hosts, "_resolve", resolve)


ALLOWED = egress.ALLOWED_PORTS


@pytest.mark.parametrize(
    ("line", "status"),
    [
        ("GET http://public.example/ HTTP/1.1", 405),  # plain http has no route
        ("CONNECT public.example:80 HTTP/1.1", 403),
        ("CONNECT public.example:8080 HTTP/1.1", 403),
        ("CONNECT private.example:443 HTTP/1.1", 403),
        ("CONNECT split.example:443 HTTP/1.1", 403),
        ("CONNECT metadata.example:443 HTTP/1.1", 403),
        ("CONNECT 169.254.169.254:443 HTTP/1.1", 403),
        ("CONNECT 127.0.0.1:443 HTTP/1.1", 403),
        ("CONNECT 10.0.0.1:22 HTTP/1.1", 403),
        ("CONNECT [::1]:443 HTTP/1.1", 403),
        ("CONNECT mapped.example:443 HTTP/1.1", 403),
        ("CONNECT public.example HTTP/1.1", 400),
    ],
)
async def test_the_proxy_refuses_every_non_public_or_non_allowed_target(
    line: str, status: int
) -> None:
    got, _reason, address, _port = await egress.vet_connect(line, ALLOWED)
    assert (got, address) == (status, "")


async def test_a_public_host_on_an_allowed_port_is_vetted_to_its_address() -> None:
    assert await egress.vet_connect("CONNECT public.example:443 HTTP/1.1", ALLOWED) == (
        200,
        "",
        PUBLIC,
        443,
    )
    assert (await egress.vet_connect("CONNECT public.example:22 HTTP/1.1", ALLOWED))[0] == 200


async def test_the_jobs_own_remote_port_is_allowed_and_no_other(tmp_path: Path) -> None:
    proxy = await egress.start_proxy(str(tmp_path / "p.sock"), 8443)
    try:
        assert proxy.allowed_ports == {443, 22, 8443}
    finally:
        await proxy.close()


async def _ask(sock: str, request: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(sock)
    writer.write(request)
    await writer.drain()
    out = await asyncio.wait_for(reader.read(4096), timeout=5)
    writer.close()
    return out


async def test_the_proxy_connects_to_the_vetted_address_not_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialled: list[tuple[str, int]] = []

    async def open_connection(host: str, port: int, **_: Any) -> Any:
        dialled.append((host, port))
        raise OSError("test: no route")

    monkeypatch.setattr(egress.asyncio, "open_connection", open_connection)
    sock = str(tmp_path / "p.sock")
    proxy = await egress.start_proxy(sock, None)
    try:
        out = await _ask(sock, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 502")
        assert dialled == [(PUBLIC, 443)]
        refused = await _ask(sock, b"CONNECT private.example:443 HTTP/1.1\r\n\r\n")
        assert refused.startswith(b"HTTP/1.1 403")
        plain = await _ask(sock, b"GET http://public.example/ HTTP/1.1\r\n\r\n")
        assert plain.startswith(b"HTTP/1.1 405")
        assert dialled == [(PUBLIC, 443)]  # nothing refused was dialled
    finally:
        await proxy.close()


async def test_an_allowed_tunnel_splices_bytes_both_ways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"echo:" + await reader.readexactly(4))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_open = asyncio.open_connection

    async def open_connection(host: str, _port: int, **kw: Any) -> Any:
        assert host == PUBLIC
        return await real_open("127.0.0.1", port, **kw)

    monkeypatch.setattr(egress.asyncio, "open_connection", open_connection)
    sock = str(tmp_path / "p.sock")
    proxy = await egress.start_proxy(sock, None)
    try:
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\nping")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert head.startswith(b"HTTP/1.1 200")
        assert await asyncio.wait_for(reader.read(100), timeout=5) == b"echo:ping"
        writer.close()
    finally:
        await proxy.close()
        server.close()
