"""The only route out of a repository-job sandbox: an HTTP CONNECT proxy.

It listens on a unix socket in the job's own directory, outside the sandbox
(the relay inside reaches it through a bind mount). Every connection is one
`CONNECT host:port` and is checked on its own, so a redirect, a submodule URL
or a package download gets the same answer:

- the port is 443, 22, or the job's own remote's port;
- the host resolves, and every address it resolves to is public
  (`server.core.hosts.vet_host`): loopback, private, link-local (the cloud
  metadata address), multicast and the like are refused;
- the proxy connects to the address it vetted, never to the name again, so a
  second DNS answer cannot point the tunnel inward.

Anything that is not CONNECT (plain http) is refused. One job holds at most
`MAX_TUNNELS` tunnels; one more waits up to `code_egress_slot_wait_s` and is refused with
a 503 (the first refusal and the job's count are logged). A tunnel with no byte either
way for `code_egress_idle_timeout_s` is closed, or for `code_egress_half_close_timeout_s`
once its client has sent EOF. The relay inside the sandbox bridges at most `MAX_TUNNELS`
connections, so the rest wait in its listen backlog.
"""

from __future__ import annotations

import asyncio
import logging
import time

from server.core.hosts import vet_host
from server.settings import settings

logger = logging.getLogger("loom.code_index.egress")

ALLOWED_PORTS = frozenset({443, 22})
MAX_TUNNELS = 32


async def vet_connect(
    request_line: str, allowed_ports: frozenset[int]
) -> tuple[int, str, str, int]:
    """(status, reason, address, port) for one request line. Status 200 means
    connect to `address`:`port`; anything else is the refusal to send."""
    parts = request_line.split()
    if len(parts) != 3 or parts[0] != "CONNECT":
        return 405, "only CONNECT is allowed", "", 0
    host, _, port_text = parts[1].rpartition(":")
    host = host.strip("[]")
    if not host or not (port_text.isascii() and port_text.isdigit()):
        return 400, "target must be host:port", "", 0
    port = int(port_text)
    if not 1 <= port <= 65535 or port not in allowed_ports:
        return 403, f"port {port} is not allowed", "", 0
    refusal, address = await vet_host(host)
    if refusal:
        return 403, refusal, "", 0
    return 200, "", address, port


async def _splice(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, last_active: list[float]
) -> None:
    """Copy `reader` to `writer` until EOF, or until neither direction of the
    tunnel has moved a byte for `code_egress_idle_timeout_s` (`last_active[0]` is shared by
    both directions, so a long download with a silent client stays open)."""
    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    reader.read(65536), timeout=settings.code_egress_idle_timeout_s
                )
            except TimeoutError:
                if time.monotonic() - last_active[0] >= settings.code_egress_idle_timeout_s:
                    return
                continue
            if not data:
                break
            last_active[0] = time.monotonic()
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def _until_server_quiet(
    server_side: asyncio.Task[None], last_active: list[float], to_client: asyncio.StreamWriter
) -> None:
    """Wait for the server's direction until `code_egress_half_close_timeout_s` passes
    with no byte either way: a server need not close a connection its client has
    finished with. Bytes still on their way to a slow client are not silence, up to
    `code_egress_idle_timeout_s`."""
    while not server_side.done():
        half_close = settings.code_egress_half_close_timeout_s
        buffered = to_client.transport.get_write_buffer_size()
        limit = settings.code_egress_idle_timeout_s if buffered else half_close
        quiet = time.monotonic() - last_active[0]
        if quiet >= limit:
            return
        await asyncio.wait({server_side}, timeout=min(limit - quiet, half_close))


class EgressProxy:
    """One job's proxy. `close()` stops it and drops every open tunnel."""

    def __init__(self, allowed_ports: frozenset[int]) -> None:
        self.allowed_ports = allowed_ports
        self.server: asyncio.Server | None = None
        self._tunnels: set[asyncio.Task[None]] = set()
        self._slots = asyncio.Semaphore(MAX_TUNNELS)
        self._refused = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tunnels.add(task)
        try:
            try:
                await asyncio.wait_for(
                    self._slots.acquire(), timeout=settings.code_egress_slot_wait_s
                )
            except TimeoutError:
                self._refused += 1
                if self._refused == 1:
                    logger.warning("egress refused a tunnel: all %d are open", MAX_TUNNELS)
                writer.write(b"HTTP/1.1 503 Refused\r\n\r\ntoo many open tunnels\n")
                return
            try:
                await self._tunnel(reader, writer)
            finally:
                self._slots.release()
        finally:
            if task is not None:
                self._tunnels.discard(task)
            writer.close()

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # not a setting: a CONNECT head, read before any job byte flows
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        status, reason, address, port = await vet_connect(line, self.allowed_ports)
        if status != 200:
            logger.warning("egress refused %r: %s", line[:200], reason)
            writer.write(f"HTTP/1.1 {status} Refused\r\n\r\n{reason}\n".encode())
            await writer.drain()
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(address, port),
                timeout=settings.code_egress_connect_timeout_s,
            )
        except (OSError, TimeoutError) as e:
            writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{type(e).__name__}\n".encode())
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        opened = time.monotonic()
        last_active = [opened]
        server_side = asyncio.create_task(_splice(up_reader, writer, last_active))
        try:
            await _splice(reader, up_writer, last_active)
            if last_active[0] != opened:  # else the client gave up before a byte
                await _until_server_quiet(server_side, last_active, writer)
        finally:
            up_writer.close()
            server_side.cancel()
            await asyncio.gather(server_side, return_exceptions=True)

    async def close(self) -> None:
        if self._refused > 1:
            logger.warning("egress refused %d tunnels at the cap of %d", self._refused, MAX_TUNNELS)
        tunnels = list(self._tunnels)
        if self.server is not None:
            self.server.close()
        for task in tunnels:
            task.cancel()
        await asyncio.gather(*tunnels, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()


async def start_proxy(path: str, remote_port: int | None) -> EgressProxy:
    """A proxy listening on the unix socket `path`, allowing 443, 22 and
    `remote_port`."""
    allowed = ALLOWED_PORTS | ({remote_port} if remote_port else set())
    proxy = EgressProxy(frozenset(allowed))
    proxy.server = await asyncio.start_unix_server(proxy.handle, path=path)
    return proxy


__all__ = ["ALLOWED_PORTS", "MAX_TUNNELS", "EgressProxy", "start_proxy", "vet_connect"]
