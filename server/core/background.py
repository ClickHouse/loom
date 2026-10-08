"""Long-lived background tasks start with an empty context.

`asyncio.create_task` copies the caller's `contextvars` into the new task.
That is right for a per-request fire-and-forget write — the exposure or
trace row should carry the request's tag — and wrong for a drain loop,
flusher or worker that a request happens to start and that then lives for
the process: every write it makes for the next hour carries that one
request's `log_comment`, request id and auth context.

`detached_task` is `create_task` with a fresh `contextvars.Context()`, so a
task started this way belongs to the process, not to the request that
happened to start it.

The event loop keeps only a weak reference to a task, so a fire-and-forget
task nobody holds can be collected mid-flight. `TaskSet.spawn` holds each task
until it is done; iterating a `TaskSet` yields the tasks still pending, for a
drain on shutdown or in a test.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from typing import Any

_registered_tasks: set[asyncio.Task[Any]] = set()
_buffer_drainers: dict[str, Callable[[], Awaitable[None]]] = {}


def register_buffer_drainer(name: str, drain: Callable[[], Awaitable[None]]) -> None:
    """Composition roots register explicit buffered-write flushes before admission."""
    _buffer_drainers[name] = drain


async def flush_registered_buffers() -> bool:
    """Missing composition is unknown, not a successfully drained buffer census."""
    if not _buffer_drainers:
        return False
    for drain in tuple(_buffer_drainers.values()):
        await drain()
    return True


def register_deferred_task[T](task: asyncio.Task[T]) -> asyncio.Task[T]:
    """Include an existing finite task in model-transition drains, without changing it."""
    if task not in _registered_tasks:
        _registered_tasks.add(task)
        task.add_done_callback(_registered_tasks.discard)
    return task


def create_deferred_task[T](
    coro: Coroutine[Any, Any, T], *, name: str | None = None
) -> asyncio.Task[T]:
    """A finite child of a request/pass, visible after its parent returns."""
    return register_deferred_task(asyncio.create_task(coro, name=name))


def detached_task[T](coro: Coroutine[Any, Any, T], *, name: str) -> asyncio.Task[T]:
    """Schedule `coro` on the running loop with an empty context: no query
    tag, no request id, no auth, no capture level inherited from the caller.
    For process-lifetime tasks only; a per-request write keeps `create_task`."""
    return asyncio.get_running_loop().create_task(coro, name=name, context=contextvars.Context())


class TaskSet:
    """Strong references to fire-and-forget tasks until each is done."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn[T](
        self, coro: Coroutine[Any, Any, T], *, name: str, detached: bool = False
    ) -> asyncio.Task[T]:
        """Schedule `coro` on the running loop (`detached`: with an empty
        context, see `detached_task`) and hold it until it is done. Raises
        RuntimeError without a running loop, like `create_task`."""
        if detached:
            t = detached_task(coro, name=name)
        else:
            t = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        # Keep the task visible even if its short-lived TaskSet owner is collected.
        register_deferred_task(t)
        return t

    def __iter__(self) -> Iterator[asyncio.Task[Any]]:
        return iter(list(self._tasks))

    def __len__(self) -> int:
        return len(self._tasks)


async def drain_task_sets(*, timeout_s: float) -> bool:
    """Wait for registered deferred tasks and their registered descendants.

    Producers must already be stopped. False means tasks remain (including work
    on another event loop), never permission to proceed. Timeout and cancellation
    do not cancel the work being drained. A task failure can be quiescent without
    being a successful write: this proves completion, not durability. Untracked
    tasks, timers, buffered writes and server-side queries require separate checks.
    """
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise ValueError("Deferred drain timeout must be finite and nonnegative")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    # A task can already be done while its callbacks are still queued. Give
    # those callbacks a turn to register descendants before the first census,
    # including a nonblocking (zero-timeout) activation check.
    await asyncio.sleep(0)
    while True:
        pending = {task for task in _registered_tasks if not task.done()}
        if not pending:
            return True
        if asyncio.current_task() in pending:
            raise RuntimeError("Cannot drain task sets from a tracked task")
        if any(task.get_loop() is not loop for task in pending):
            return False
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        _, still_pending = await asyncio.wait(pending, timeout=remaining)
        if still_pending:
            return False
        # Include descendants spawned by completion callbacks as well as by the
        # coroutine body. Every such task must use a registered TaskSet too.
        await asyncio.sleep(0)


__all__ = ["TaskSet", "detached_task", "drain_task_sets", "register_deferred_task"]
