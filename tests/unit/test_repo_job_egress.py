"""The egress proxy is the repository-job sandbox's only route out: CONNECT only,
to ports 443, 22 and the job's remote port, to hosts whose every address is
public, and always to the address it vetted (never the name again)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
import sys
from collections.abc import Awaitable, Callable
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


async def test_the_jobs_own_remote_port_is_allowed_and_no_other(sock_dir: Path) -> None:
    proxy = await egress.start_proxy(str(sock_dir / "p.sock"), 8443)
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
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialled: list[tuple[str, int]] = []

    async def open_connection(host: str, port: int, **_: Any) -> Any:
        dialled.append((host, port))
        raise OSError("test: no route")

    monkeypatch.setattr(egress.asyncio, "open_connection", open_connection)
    sock = str(sock_dir / "p.sock")
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
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
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
    sock = str(sock_dir / "p.sock")
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
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
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
    sock = str(sock_dir / "p.sock")
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
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Waiting without a bound would let a job park any number of connections
    (two fds each) in the workers process."""
    caplog.set_level(logging.WARNING, logger="loom.code_index.egress")
    gate = asyncio.Event()

    async def slow_vet(host: str) -> tuple[str, str]:
        await gate.wait()
        return "refused", ""

    monkeypatch.setattr(egress, "vet_host", slow_vet)
    monkeypatch.setattr(egress, "MAX_TUNNELS", 1)
    monkeypatch.setattr(settings, "code_egress_slot_wait_s", 0.2)
    sock = str(sock_dir / "p.sock")
    proxy = await egress.start_proxy(sock, None)
    try:
        _r1, first = await asyncio.open_unix_connection(sock)
        first.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        await asyncio.sleep(0.05)
        for _ in range(2):
            second, w2 = await asyncio.open_unix_connection(sock)
            answer = await asyncio.wait_for(second.read(), timeout=5)
            assert answer.startswith(b"HTTP/1.1 503")
            w2.close()
        first.close()
    finally:
        await proxy.close()
    logged = [r.getMessage() for r in caplog.records if r.name == "loom.code_index.egress"]
    assert logged == [
        "egress refused a tunnel: all 1 are open",
        "egress refused 2 tunnels at the cap of 1",
    ]


async def test_a_tunnel_with_no_bytes_either_way_is_closed(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
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
    sock = str(sock_dir / "p.sock")
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


async def _tunnel_to(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    sock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[egress.EgressProxy, asyncio.Server, asyncio.StreamReader, asyncio.StreamWriter]:
    """A proxy whose every tunnel reaches `handler`, and one open tunnel through it."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_open = asyncio.open_connection

    async def open_connection(_host: str, _port: int, **kw: Any) -> Any:
        return await real_open("127.0.0.1", port, **kw)

    monkeypatch.setattr(egress.asyncio, "open_connection", open_connection)
    sock = str(sock_dir / "p.sock")
    proxy = await egress.start_proxy(sock, None)
    try:
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert head.startswith(b"HTTP/1.1 200")
    except BaseException:
        await proxy.close()
        server.close()
        raise
    return proxy, server, reader, writer


async def test_a_tunnel_whose_client_sent_eof_closes_when_the_server_goes_quiet(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry need not close a connection its client has finished with; the
    tunnel would otherwise hold its slot for the whole idle timeout."""
    done = asyncio.Event()

    async def never_closes(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.write(b"bye")
        await done.wait()
        writer.close()

    monkeypatch.setattr(settings, "code_egress_half_close_timeout_s", 1.0)
    proxy, server, reader, writer = await _tunnel_to(never_closes, sock_dir, monkeypatch)
    try:
        writer.write(b"request")
        writer.write_eof()
        # the server's last bytes still arrive, then the proxy hangs up
        assert await asyncio.wait_for(reader.read(), timeout=5) == b"bye"
        writer.close()
    finally:
        done.set()
        await proxy.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_a_server_still_sending_after_the_clients_eof_keeps_its_tunnel(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = asyncio.Event()

    async def streams(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        for chunk in b"0123456789abcdefghij":  # two half-close timeouts of steady bytes
            writer.write(bytes([chunk]))
            await asyncio.sleep(0.05)
        await done.wait()
        writer.close()

    monkeypatch.setattr(settings, "code_egress_half_close_timeout_s", 0.5)
    proxy, server, reader, writer = await _tunnel_to(streams, sock_dir, monkeypatch)
    try:
        writer.write(b"request")
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), timeout=5) == b"0123456789abcdefghij"
        writer.close()
    finally:
        done.set()
        await proxy.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_a_slow_client_after_its_eof_still_gets_the_whole_response(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxy blocks writing to a client that does not read yet; that is not a
    quiet server."""
    done = asyncio.Event()
    body = b"x" * (8 * 1024 * 1024)

    async def sends_a_lot(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.write(body)
        await writer.drain()
        await done.wait()
        writer.close()

    monkeypatch.setattr(settings, "code_egress_half_close_timeout_s", 0.3)
    proxy, server, reader, writer = await _tunnel_to(sends_a_lot, sock_dir, monkeypatch)
    try:
        writer.write(b"request")
        writer.write_eof()
        await asyncio.sleep(1.0)  # past three half-close timeouts before reading
        assert len(await asyncio.wait_for(reader.read(), timeout=10)) == len(body)
        writer.close()
    finally:
        done.set()
        await proxy.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def _connect_answer(sock: str) -> tuple[bytes, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(sock)
    writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5), writer


async def test_a_client_that_never_reads_frees_its_slot_at_the_idle_timeout(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = asyncio.Event()

    async def sends_a_lot(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.write(b"x" * (8 * 1024 * 1024))
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        await done.wait()
        writer.close()

    monkeypatch.setattr(egress, "MAX_TUNNELS", 1)
    monkeypatch.setattr(settings, "code_egress_slot_wait_s", 0.2)
    monkeypatch.setattr(settings, "code_egress_half_close_timeout_s", 0.2)
    monkeypatch.setattr(settings, "code_egress_idle_timeout_s", 3.0)
    proxy, server, _reader, writer = await _tunnel_to(sends_a_lot, sock_dir, monkeypatch)
    sock = str(sock_dir / "p.sock")
    try:
        writer.write(b"request")
        writer.write_eof()  # and never reads
        await asyncio.sleep(1.0)
        head, refused = await _connect_answer(sock)
        refused.close()
        assert head.startswith(b"HTTP/1.1 503")  # past the half-close timeout, bytes wait
        await asyncio.sleep(2.5)
        head, second = await _connect_answer(sock)
        second.close()
        assert head.startswith(b"HTTP/1.1 200")  # the idle timeout freed the slot
        done.set()
        await asyncio.wait_for(proxy.close(), timeout=5)  # the stuck client is dropped
    finally:
        writer.close()
        done.set()
        await proxy.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_close_ends_a_tunnel_in_its_half_close_wait(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = asyncio.Event()
    client_eof = asyncio.Event()

    async def quiet(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        client_eof.set()
        await done.wait()
        writer.close()

    monkeypatch.setattr(settings, "code_egress_half_close_timeout_s", 30.0)
    proxy, server, reader, writer = await _tunnel_to(quiet, sock_dir, monkeypatch)
    try:
        writer.write(b"hello")  # a byte, so the proxy waits for the server
        writer.write_eof()
        await asyncio.wait_for(client_eof.wait(), timeout=5)  # in the half-close wait
        await asyncio.wait_for(proxy.close(), timeout=5)
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        writer.close()
    finally:
        done.set()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_a_client_that_gives_up_before_a_byte_frees_its_slot_at_once(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection a client abandoned in the relay's backlog: its CONNECT and then
    EOF. A TLS server says nothing until it has a ClientHello."""
    done = asyncio.Event()

    async def tls_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        await done.wait()
        writer.close()

    monkeypatch.setattr(settings, "code_egress_half_close_timeout_s", 30.0)
    proxy, server, reader, writer = await _tunnel_to(tls_server, sock_dir, monkeypatch)
    try:
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        writer.close()
    finally:
        done.set()
        await proxy.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_a_server_that_resets_ends_the_tunnel(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load balancer may drop an idle keep-alive with a reset; the client, silent in
    its pool, must not hold the slot until the idle timeout."""

    async def resets(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(7)
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.close()

    proxy, server, reader, writer = await _tunnel_to(resets, sock_dir, monkeypatch)
    try:
        writer.write(b"request")
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        writer.close()
    finally:
        await asyncio.wait_for(proxy.close(), timeout=5)
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_a_client_that_gives_up_while_its_name_is_vetted_is_not_dialled(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialled = []

    async def slow_vet(host: str) -> tuple[str, str]:
        await asyncio.sleep(0.2)
        return "", PUBLIC

    async def open_connection(host: str, port: int, **kw: Any) -> Any:
        dialled.append(host)
        raise OSError("not reached")

    monkeypatch.setattr(egress, "vet_host", slow_vet)
    monkeypatch.setattr(egress.asyncio, "open_connection", open_connection)
    sock = str(sock_dir / "p.sock")
    proxy = await egress.start_proxy(sock, None)
    try:
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        assert dialled == []
        writer.close()
    finally:
        await proxy.close()


@pytest.mark.skipif(
    sys.platform != "linux", reason="a unix socket closed with data unread resets on Linux"
)
async def test_a_client_that_resets_ends_the_tunnel(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unix socket closed with unread data resets its peer; the proxy must not then
    wait out the half-close timeout with the slot held."""
    done = asyncio.Event()

    async def sends_then_waits(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(7)
        writer.write(b"unread")
        await done.wait()
        writer.close()

    monkeypatch.setattr(egress, "MAX_TUNNELS", 1)
    monkeypatch.setattr(settings, "code_egress_slot_wait_s", 2.0)
    monkeypatch.setattr(settings, "code_egress_half_close_timeout_s", 30.0)
    proxy, server, _reader, writer = await _tunnel_to(sends_then_waits, sock_dir, monkeypatch)
    try:
        writer.transport.pause_reading()  # "unread" stays in the kernel's queue
        writer.write(b"request")
        await asyncio.sleep(0.3)  # for it to arrive there
        writer.transport.abort()  # closed with data unread: a reset
        head, second = await _connect_answer(str(sock_dir / "p.sock"))
        second.close()
        assert head.startswith(b"HTTP/1.1 200")  # the slot came back before the 30 s
    finally:
        done.set()
        await asyncio.wait_for(proxy.close(), timeout=5)
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_close_ends_an_open_tunnel_and_its_server_side(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_eof = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        upstream_eof.set()
        writer.close()

    proxy, server, _reader, writer = await _tunnel_to(silent, sock_dir, monkeypatch)
    try:
        await asyncio.wait_for(proxy.close(), timeout=5)
        await asyncio.wait_for(upstream_eof.wait(), timeout=5)
    finally:
        writer.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)
