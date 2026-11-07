"""The egress proxy is the repository-job sandbox's only route out: CONNECT only,
to ports 443, 22 and the job's remote port, to hosts whose every address is
public, and always to the address it vetted (never the name again)."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from server.code.app.index import egress
from server.settings import settings
from tests.unit._hosts import resolves

PUBLIC = "93.184.215.14"
NAMES = {
    "public.example": [PUBLIC],
    "private.example": ["10.0.0.5"],
    "split.example": [PUBLIC, "192.168.1.7"],  # one private answer is enough to refuse
    "metadata.example": ["169.254.169.254"],
    "mapped.example": ["::ffff:127.0.0.1"],
}


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A short directory for the proxy's socket: an AF_UNIX path is capped near 104
    bytes on macOS, and pytest's default temporary directory there is longer."""
    short = Path(tempfile.mkdtemp(prefix="egress-", dir="/tmp"))
    yield short
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture(autouse=True)
def _resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str) -> list[str]:
        if host in NAMES:
            return NAMES[host]
        return [host]  # an address literal resolves to itself

    resolves(monkeypatch, resolve)


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
        ("CONNECT public.example:\xb2 HTTP/1.1", 400),  # a digit to isdigit(), not to int()
        ("CONNECT public.example:0 HTTP/1.1", 403),
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


async def test_a_port_out_of_range_is_refused_even_when_the_remote_names_it() -> None:
    # remote_port() reads `https://host:70000/` as 70000; the dial would raise.
    status, _reason, _address, _port = await egress.vet_connect(
        "CONNECT public.example:70000 HTTP/1.1", ALLOWED | {70000}
    )
    assert status == 403


async def test_a_job_holds_at_most_max_tunnels_and_close_ends_every_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each waiting connection would otherwise put a getaddrinfo on the shared
    thread pool; close() returns only once every handler has finished."""
    in_flight = 0
    gate = asyncio.Event()

    async def slow_vet(host: str) -> tuple[str, str]:
        nonlocal in_flight
        in_flight += 1
        await gate.wait()
        return "refused", ""

    monkeypatch.setattr(egress, "vet_host", slow_vet)
    sock = str(tmp_path / "p.sock")
    proxy = await egress.start_proxy(sock, None)
    writers = []
    for _ in range(egress.MAX_TUNNELS + 20):
        _reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        writers.append(writer)
    await asyncio.sleep(0.3)
    assert in_flight == egress.MAX_TUNNELS
    await proxy.close()
    assert not proxy._tunnels
    for writer in writers:
        writer.close()


async def test_a_connection_past_the_tunnel_cap_is_refused_after_a_short_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiting without a bound would let a job park any number of connections
    (two fds each) in the workers process."""
    gate = asyncio.Event()

    async def slow_vet(host: str) -> tuple[str, str]:
        await gate.wait()
        return "refused", ""

    monkeypatch.setattr(egress, "vet_host", slow_vet)
    monkeypatch.setattr(egress, "MAX_TUNNELS", 1)
    monkeypatch.setattr(settings, "code_egress_slot_wait_s", 0.2)
    sock = str(tmp_path / "p.sock")
    proxy = await egress.start_proxy(sock, None)
    try:
        _r1, first = await asyncio.open_unix_connection(sock)
        first.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        await asyncio.sleep(0.05)
        second, w2 = await asyncio.open_unix_connection(sock)
        answer = await asyncio.wait_for(second.read(), timeout=5)
        assert answer.startswith(b"HTTP/1.1 503")
        first.close()
        w2.close()
    finally:
        await proxy.close()


async def test_a_tunnel_with_no_bytes_either_way_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # never answers

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_open = asyncio.open_connection

    async def open_connection(_host: str, _port: int, **kw: Any) -> Any:
        return await real_open("127.0.0.1", port, **kw)

    monkeypatch.setattr(egress.asyncio, "open_connection", open_connection)
    monkeypatch.setattr(settings, "code_egress_idle_timeout_s", 0.2)
    sock = str(tmp_path / "p.sock")
    proxy = await egress.start_proxy(sock, None)
    try:
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert head.startswith(b"HTTP/1.1 200")
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""  # the proxy hung up
        writer.close()
    finally:
        await proxy.close()
        server.close()
