"""Network and logging boundaries for the outbound MCP client."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import dns.asyncresolver
import dns.exception

_PRIVATE = ContextVar("mcp_private_work", default=False)
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class _PrivateFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE.get()


_PRIVATE_FILTER = _PrivateFilter()


@contextmanager
def private_work() -> Iterator[None]:
    """Drop SDK diagnostics in this context and tasks spawned from it."""
    root = logging.getLogger()
    handlers = set(root.handlers)
    if logging.lastResort is not None:
        handlers.add(logging.lastResort)
    for logger in root.manager.loggerDict.copy().values():
        if isinstance(logger, logging.Logger):
            handlers.update(logger.handlers)
    for handler in handlers:
        handler.addFilter(_PRIVATE_FILTER)
    token = _PRIVATE.set(True)
    try:
        yield
    finally:
        _PRIVATE.reset(token)


def _is_public(address: _Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address in _NAT64 or (1 < int(address) < 2**32):
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address.is_global and not address.is_multicast


async def resolve_public_address(host: str) -> _Address | None:
    """Vet all A and AAAA answers, then return one literal to pin for the session."""
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = []
    if not addresses:
        try:
            resolver = dns.asyncresolver.Resolver()
            answers = await asyncio.gather(
                *(
                    resolver.resolve(host, family, search=False, raise_on_no_answer=False)
                    for family in ("A", "AAAA")
                ),
                return_exceptions=True,
            )
            for answer in answers:
                if isinstance(answer, asyncio.CancelledError):
                    raise answer
                if isinstance(answer, BaseException):
                    return None
                addresses.extend(ipaddress.ip_address(record.to_text()) for record in answer)
        except (ValueError, OSError, dns.exception.DNSException):
            return None
    return addresses[0] if addresses and all(map(_is_public, addresses)) else None
