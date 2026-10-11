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

Anything that is not CONNECT (plain http) is refused.
"""

from __future__ import annotations

import asyncio
import logging

from server.core.hosts import vet_host

logger = logging.getLogger("loom.code_index.egress")

ALLOWED_PORTS = frozenset({443, 22})
_CONNECT_TIMEOUT_S = 15.0


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
    if not host or not port_text.isdigit():
        return 400, "target must be host:port", "", 0
    port = int(port_text)
    if port not in allowed_ports:
        return 403, f"port {port} is not allowed", "", 0
    refusal, address = await vet_host(host)
    if refusal:
        return 403, refusal, "", 0
    return 200, "", address, port


async def _splice(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


class EgressProxy:
    """One job's proxy. `close()` stops it and drops every open tunnel."""

    def __init__(self, allowed_ports: frozenset[int]) -> None:
        self.allowed_ports = allowed_ports
        self.server: asyncio.Server | None = None
        self._tunnels: set[asyncio.Task[None]] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tunnels.add(task)
        try:
            await self._tunnel(reader, writer)
        finally:
            if task is not None:
                self._tunnels.discard(task)
            writer.close()

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
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
                asyncio.open_connection(address, port), timeout=_CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError) as e:
            writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{type(e).__name__}\n".encode())
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            await asyncio.gather(_splice(reader, up_writer), _splice(up_reader, writer))
        finally:
            up_writer.close()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
        for task in list(self._tunnels):
            task.cancel()


async def start_proxy(path: str, remote_port: int | None) -> EgressProxy:
    """A proxy listening on the unix socket `path`, allowing 443, 22 and
    `remote_port`."""
    allowed = ALLOWED_PORTS | ({remote_port} if remote_port else set())
    proxy = EgressProxy(frozenset(allowed))
    proxy.server = await asyncio.start_unix_server(proxy.handle, path=path)
    return proxy


__all__ = ["ALLOWED_PORTS", "EgressProxy", "start_proxy", "vet_connect"]
