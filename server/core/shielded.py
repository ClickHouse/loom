"""Writes a cancelled request must still finish, held until done; shutdown drains them
before the ClickHouse client goes."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from server.core.background import TaskSet

_running = TaskSet()


async def shielded[T](write: Coroutine[Any, Any, T], *, name: str) -> T:
    """Run `write` to its end even when the request awaiting it is cancelled,
    again and again (anyio repeats a cancellation), and return what it returns:
    the task is held here and the wait for it is shielded."""
    return await asyncio.shield(_running.spawn(write, name=name))


async def to_its_end[T](write: Coroutine[Any, Any, T], *, name: str) -> T:
    """`shielded`, and the caller waits for the end too: a cancellation, however often it
    comes, is raised only once `write` has finished, so whatever the caller holds (a
    claim) is held until then."""
    task = _running.spawn(write, name=name)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as e:
            if task.done():  # `write` ended (or was cancelled itself): the cancel goes on
                raise
            cancelled = e
    if cancelled is not None:
        raise cancelled
    return result


async def drain(timeout_s: float = 5.0) -> list[str]:
    """Wait for the shielded writes still running (at most `timeout_s`);
    returns the names of those left running."""
    live = [t for t in _running if not t.done()]
    if not live:
        return []
    _done, pending = await asyncio.wait(live, timeout=timeout_s)
    return sorted(t.get_name() for t in pending)
