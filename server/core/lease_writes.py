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


def _work_deadlines(held: list[leases.Lease], ttl: float) -> tuple[float, float]:
    # Sample both clocks before work_left: a suspension cannot extend the holder.
    loop_now = asyncio.get_running_loop().time()
    send_now = time.monotonic()
    left = min(leases.work_left(lease, ttl) for lease in held)
    if left <= 0:
        raise FenceUnavailableError("the write lease expired")
    return loop_now + left, send_now + left


async def _admit(names: list[str], ttl: float, ch: Any, wait_s: float) -> list[leases.Lease]:
    if wait_s <= 0:
        return await _take_all(names, ttl, ch)

    loop = asyncio.get_running_loop()
    loop_until = loop.time() + wait_s
    until = time.monotonic() + wait_s
    deadline = asyncio.timeout_at(loop_until)
    held: list[leases.Lease] = []
    admitted = False

    def left() -> float:
        return min(until - time.monotonic(), loop_until - loop.time())

    try:
        async with deadline:
            while True:
                if left() <= 0:
                    raise FenceUnavailableError("write fence admission exceeded its deadline")
                try:
                    held = await _take_all(names, ttl, ch)
                except FenceUnavailableError:
                    remaining = left()
                    if remaining <= 0:
                        raise
                    # Release partial batches before waiting for overlapping keys.
                    await asyncio.sleep(min(0.1, remaining))
                    continue
                # A completed take need not yield to the timeout after suspension.
                if left() <= 0:
                    raise FenceUnavailableError("write fence admission exceeded its deadline")
                break
        admitted = True
        return held
    except TimeoutError:
        if not deadline.expired():
            raise
        raise FenceUnavailableError("write fence admission exceeded its deadline") from None
    finally:
        if held and not admitted:
            await to_its_end(_release_all(held), name="lease_writes.release")


@asynccontextmanager
async def fenced_many(
    names: list[str], *, ch: Any = None, wait_s: float = 0.0
) -> AsyncIterator[Callable[[bool], None]]:
    """Admit keys in bounded waves and fence sends before the earliest holder expires."""
    ttl = lease_ttl_s(rmt_carry.send_bound_s())
    held = await _admit(names, ttl, ch, wait_s)
    uncertain = False

    def mark_uncertain(value: bool) -> None:
        nonlocal uncertain
        uncertain = value

    try:
        loop_deadline, send_deadline = _work_deadlines(held, ttl)
        deadline = asyncio.timeout_at(loop_deadline)
        try:
            async with deadline:
                with rmt_carry.under_lease(send_deadline):
                    yield mark_uncertain
        except TimeoutError:
            if not deadline.expired():
                raise
            raise BackendUnavailableError(
                "the fenced write exceeded its deadline; retry shortly",
                operation="leased_write",
                outcome_unknown=uncertain,
            ) from None
    finally:
        if uncertain:
            for lease in held:
                lease.let_expire()
        await to_its_end(_release_all(held), name="lease_writes.release")
