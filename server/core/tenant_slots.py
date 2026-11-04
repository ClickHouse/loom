"""Each org's in-flight requests in this process, capped at its share of
LOOM_TENANT_MAX_CONCURRENT_REQUESTS. The transports hold a slot around every
op (`server.transport.gates.tenant_slot`); a route outside the op registry that
does comparable work (an upload) holds one around its own."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from server.core.errors import RateLimitedError
from server.settings import settings

# Requests each org has running in this process (org -> count).
_IN_FLIGHT: dict[str, int] = {}


def inflight_cap() -> int:
    """This process's share of LOOM_TENANT_MAX_CONCURRENT_REQUESTS, divided the way
    the rate limits are (replicas × web workers, at least 1); 0 = no cap."""
    configured = int(settings.tenant_max_concurrent_requests)
    if configured <= 0:
        return 0
    from server.core.ratelimit import effective_divisor

    return max(1, configured // effective_divisor(shared=False))


@contextmanager
def hold(org: str | None) -> Iterator[None]:
    """Hold one of `org`'s in-flight slots, released however the block ends.
    RateLimitedError (429 RATE_LIMITED) when the org's share is all in use."""
    cap = inflight_cap()
    if not org or cap <= 0:
        yield
        return
    if _IN_FLIGHT.get(org, 0) >= cap:
        raise RateLimitedError(
            f"org {org!r} already has {cap} requests running on this server; retry shortly",
            retry_after_s=1,
        )
    _IN_FLIGHT[org] = _IN_FLIGHT.get(org, 0) + 1
    try:
        yield
    finally:
        left = _IN_FLIGHT[org] - 1
        if left > 0:
            _IN_FLIGHT[org] = left
        else:
            del _IN_FLIGHT[org]
