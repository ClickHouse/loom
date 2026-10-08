"""code.backport_gap's per-namespace pools (presence, patch, history) settle through one
helper that owns its tasks."""

from __future__ import annotations

import asyncio

from server.code.app import backport as h


async def test_settle_cancels_every_task_when_its_caller_is_cancelled() -> None:
    """The three per-namespace pools (presence, patch, history) settle through one
    helper: past the wall the slow tasks are cancelled and returned, and a cancel of the
    caller itself cancels them all instead of leaving git reads running."""

    async def slow() -> None:
        await asyncio.sleep(30)

    async def fast() -> str:
        return "ok"

    a, b = asyncio.ensure_future(fast()), asyncio.ensure_future(slow())
    pending = await h._settle([a, b], wall_ms=50)
    assert pending == {b} and b.cancelled() and a.result() == "ok"

    tasks = [asyncio.ensure_future(slow()) for _ in range(3)]
    outer = asyncio.ensure_future(h._settle(tasks, wall_ms=30_000))
    await asyncio.sleep(0)
    outer.cancel()
    try:
        await outer
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)
    assert all(t.cancelled() for t in tasks)
