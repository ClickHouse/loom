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


async def drain(timeout_s: float = 5.0) -> list[str]:
    """Wait for the shielded writes still running (at most `timeout_s`);
    returns the names of those left running."""
    live = [t for t in _running if not t.done()]
    if not live:
        return []
    _done, pending = await asyncio.wait(live, timeout=timeout_s)
    return sorted(t.get_name() for t in pending)
