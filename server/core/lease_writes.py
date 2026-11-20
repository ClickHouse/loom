"""Bound server sends before an unrenewed lease can be taken over."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from server.core import leases, rmt_carry
from server.core.errors import BackendUnavailableError
from server.core.shielded import to_its_end
from server.settings import settings


class FenceUnavailableError(BackendUnavailableError):
    """The writer cannot enter because its holder is busy or its lease expired."""


def _wave_size() -> int:
    cap = settings.ch_write_fence_concurrency
    return min(cap, settings.ch_pool_maxsize) if settings.ch_pool_maxsize > 0 else cap


def lease_ttl_s(send_bound_s: float) -> float:
    """Keep the last admitted send's finish bound before an unrenewed lease expires."""
    return max(60.0, (send_bound_s + 5.0) / 0.6)


@asynccontextmanager
async def fenced(name: str, *, ch: Any = None) -> AsyncIterator[Callable[[bool], None]]:
    """Mark uncertain before a send and clear only after its completion is known.

    An uncertain send retains the durable holder until every admitted server send
    has finished or expired. Every eraser of the written table takes this lease too.
    """
    async with fenced_many([name], ch=ch) as mark:
        yield mark


async def _take_all(names: list[str], ttl: float, ch: Any) -> list[leases.Lease]:
    async def take(name: str) -> leases.Lease | BaseException | None:
        try:
            return await leases.take(name, ttl_s=ttl, ch=ch)
        except BaseException as error:  # noqa: BLE001 — preserve the first backend refusal, including cancellation
            return error

    held: list[leases.Lease] = []
    try:
        size = _wave_size()
        for start in range(0, len(names), size):
            tasks = [asyncio.create_task(take(name)) for name in names[start : start + size]]
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                settled = await asyncio.gather(*tasks, return_exceptions=True)
                held.extend(
                    r for r in settled if r is not None and not isinstance(r, BaseException)
                )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
                if result is None:
                    raise FenceUnavailableError("a previous leased write is still settling")
        return held
    except BaseException:
        await to_its_end(_release_all(held), name="lease_writes.release")
        raise


async def _release_all(held: list[leases.Lease]) -> None:
    size = _wave_size()
    for start in range(0, len(held), size):
        await asyncio.gather(
            *(leases.release_shielded(lease) for lease in held[start : start + size])
        )


@asynccontextmanager
async def fenced_many(names: list[str], *, ch: Any = None) -> AsyncIterator[Callable[[bool], None]]:
    """Admit keys in bounded waves and fence sends before the earliest holder expires."""
    ttl = lease_ttl_s(rmt_carry.send_bound_s())
    held = await _take_all(names, ttl, ch)
    uncertain = False

    def mark_uncertain(value: bool) -> None:
        nonlocal uncertain
        uncertain = value

    try:
        left = min(leases.work_left(lease, ttl) for lease in held)
        if left <= 0:
            raise FenceUnavailableError("the write lease expired")
        async with asyncio.timeout(left):
            with rmt_carry.under_lease(time.monotonic() + left):
                yield mark_uncertain
    finally:
        if uncertain:
            for lease in held:
                lease.let_expire()
        await to_its_end(_release_all(held), name="lease_writes.release")
