"""Eventual-consistency helper for the integration suite.

memory.set's embedding-row insert uses async_insert=1,
wait_for_async_insert=0 in
server/memory/store/inserts.py::insert_embedding_rows.
Production-correct: ~30-50ms latency win per write, and embedding
rows are a denormalized derivative of loom.memory (the source of
truth for conflict checks), so eventual consistency is safe in
production. Tests that read embeddings or search hits immediately
after a write race the CH async-flush interval (~1s default).

This helper bridges the race without coupling tests to wall-clock
sleeps. Conflict-detection paths under V3 also probe
memory_embeddings (server/memory/app/conflicts.py) — back-to-back sets may not
see each other's embeddings without bridging, same fix applies.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any


async def overrides_adopted() -> None:
    """An admin override binds the worker that took it at once and every other
    process within one refresh of loom.runtime_overrides; wait out that bound."""
    refresh = float(os.environ.get("LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS", "10"))
    await asyncio.sleep(max(1.0, refresh) + 1.0)


# `max_wait` rather than `timeout` so ruff's ASYNC109 doesn't fire —
# the rule asks async functions to not accept a `timeout` parameter
# because callers should use `asyncio.timeout(...)` outside. That
# doesn't fit here: the timeout governs an internal polling loop's
# deadline, not a hard cancellation budget, so we just use a
# different name.
async def eventually[T](
    predicate: Callable[[], Awaitable[T]],
    *,
    max_wait: float = 15.0,
    interval: float = 0.1,
    description: str = "predicate to hold",
) -> T:
    """Poll an async `predicate` until it returns truthy or `max_wait`.

    Returns the last (truthy) value observed. Raises AssertionError
    on timeout with the last observed value attached for debugging.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    last_value: Any = None
    while loop.time() < deadline:
        last_value = await predicate()
        if last_value:
            return last_value
        await asyncio.sleep(interval)
    raise AssertionError(
        f"eventually({description!r}) timed out after {max_wait}s; "
        f"last observed value = {last_value!r}"
    )


def wait_until[T](
    predicate: Callable[[], T],
    *,
    max_wait: float = 15.0,
    interval: float = 0.1,
    description: str = "predicate to hold",
) -> T:
    """The synchronous `eventually`, for sync tests and fixtures.

    A predicate that raises counts as not yet true; the last exception is
    reported on timeout.
    """
    deadline = time.monotonic() + max_wait
    last_value: Any = None
    while True:
        try:
            last_value = predicate()
        except Exception as e:  # noqa: BLE001 — a not-yet-ready resource may raise anything
            last_value = e
        else:
            if last_value:
                return last_value
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"wait_until({description!r}) timed out after {max_wait}s; "
                f"last observed value = {last_value!r}"
            )
        time.sleep(interval)
