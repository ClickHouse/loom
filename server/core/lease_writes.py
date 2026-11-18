"""Bound server sends before an unrenewed lease can be taken over."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from server.core import leases, rmt_carry
from server.core.errors import BackendUnavailableError


@asynccontextmanager
async def fenced(name: str, *, ch: Any = None) -> AsyncIterator[Callable[[bool], None]]:
    """Mark uncertain before a send and clear only after its completion is known.

    An uncertain send retains the durable holder until every admitted server send
    has finished or expired. Every eraser of the written table takes this lease too.
    """
    ttl = max(60.0, (rmt_carry.send_bound_s() + 5.0) / 0.6)
    lease = await leases.take(name, ttl_s=ttl, ch=ch)
    if lease is None:
        raise BackendUnavailableError("a previous leased write is still settling")
    uncertain = False

    def mark_uncertain(value: bool) -> None:
        nonlocal uncertain
        uncertain = value

    try:
        left = leases.work_left(lease, ttl)
        if left <= 0:
            raise BackendUnavailableError("the write lease expired")
        async with asyncio.timeout(left):
            with rmt_carry.under_lease(time.monotonic() + left):
                yield mark_uncertain
    finally:
        if uncertain:
            lease.let_expire()
        await leases.release_shielded(lease)
