"""A tracker source's host must be public before loom connects to it.

It resolves, and every address it resolves to is globally reachable
(`ipaddress.is_global`, not multicast) once an IPv4 address carried inside
IPv6 (mapped, compatible, NAT64) is unwrapped, unless the deployment allows
private hosts (a dev stack). The caller connects to the address vetted here,
so a second lookup cannot swap in another.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from server.settings import settings

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


async def vet_host(host: str) -> tuple[str, str]:
    """(refusal, address): refusal is "" when the host is acceptable, else one
    sentence saying why not; address is the validated one to connect to ("" when
    refused, or when private hosts are allowed and the name is used as given)."""
    h = (host or "").strip()
    if not h:
        return "host is required", ""
    try:
        addrs = await asyncio.to_thread(_resolve, h)
    except Exception as e:  # noqa: BLE001 — a name that does not resolve
        return f"{h} does not resolve ({type(e).__name__})", ""
    if not addrs:
        return f"{h} does not resolve", ""
    if bool(settings.tracker_self_service_allow_private_hosts):
        return "", ""
    for a in addrs:
        try:
            public = _is_public(a)
        except ValueError:
            return f"{h} resolved to {a!r}, not an address", ""
        if not public:
            return (
                f"{h} resolves to {a}, a private or local address; loom reaches only public "
                "hosts (LOOM_TRACKER_SELF_SERVICE_ALLOW_PRIVATE_HOSTS lifts this on a dev stack)"
            ), ""
    return "", addrs[0]
