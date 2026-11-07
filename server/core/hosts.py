"""A host must be public before loom connects to it on a caller's behalf.

It resolves, and every address it resolves to is globally reachable
(`ipaddress.is_global`, not multicast) once an IPv4 address carried inside
IPv6 (mapped, compatible, NAT64) is unwrapped. The caller connects to the
address vetted here, so a second lookup cannot swap in another. Tracker
sources, task and memory webhooks and the repository-job egress proxy share
this gate.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from concurrent.futures import Executor

# The NAT64 well-known prefix: its low 32 bits are the IPv4 address a packet reaches
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The IPv4 address an IPv6 one carries (mapped `::ffff:a.b.c.d`,
    compatible `::a.b.c.d`, NAT64 `64:ff9b::a.b.c.d`), else `ip` itself."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64 or (int(ip) >> 32 == 0 and int(ip) > 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _is_public(address: str) -> bool:
    ip = _embedded_ipv4(ipaddress.ip_address(address))
    return bool(ip.is_global) and not ip.is_multicast


def _resolve(host: str) -> list[str]:
    """The addresses `host` resolves to, in the resolver's order of preference."""
    infos = socket.getaddrinfo(host, None)
    return list(dict.fromkeys(str(i[4][0]) for i in infos))


async def vet_host(
    host: str,
    *,
    allow_private: bool = False,
    name_address: bool = True,
    pool: Executor | None = None,
) -> tuple[str, str]:
    """(refusal, address): refusal is "" when the host is acceptable, else one
    sentence saying why not; address is the validated one to connect to ("" when
    refused, or when `allow_private` (a dev stack) uses the name as given).
    `name_address=False` keeps the resolved address out of the refusal, for a
    caller whose refusal reaches someone who should not learn it. The lookup runs
    on `pool` (None: asyncio's default one), so a caller whose names a tenant
    chooses keeps its never-answering lookups off the shared pool."""
    h = (host or "").strip()
    if not h:
        return "host is required", ""
    try:
        if pool is None:
            addrs = await asyncio.to_thread(_resolve, h)
        else:
            addrs = await asyncio.get_running_loop().run_in_executor(pool, _resolve, h)
    except Exception as e:  # noqa: BLE001 — a name that does not resolve
        return f"{h} does not resolve ({type(e).__name__})", ""
    if not addrs:
        return f"{h} does not resolve", ""
    if allow_private:
        return "", ""
    for a in addrs:
        try:
            public = _is_public(a)
        except ValueError:
            if name_address:
                return f"{h} resolved to {a!r}, not an address", ""
            return f"{h} did not resolve to an IP address", ""
        if not public:
            named = f" to {a}," if name_address else " to"
            return (
                f"{h} resolves{named} a private or local address; loom reaches only public hosts"
            ), ""
    return "", addrs[0]
