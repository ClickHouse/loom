"""Side effects of a committed write, written beside the response.

A point write's own rows (`loom.memory` + `loom.memory_embeddings`) are the
read-your-write contract and stay awaited. What the write *projects* — the
entity graph rows of `memory.set`, the BM25 statistics of a late row — is
fail-open, repaired by a worker or a reindex when it is lost, and read by
nothing on the write's own path, so it runs after the response: each is a few
more inserts, seconds of them when the service's inserts are slow.

`schedule` runs such a coroutine as a task of the request (it keeps the
request's context, so its statements carry the request's `log_comment`); the
set holds the live tasks of the running loop, and `drain()` at shutdown lets
them land before the ClickHouse client goes away. A task scheduled with a
`scope` (org, namespace) can be waited on by a read of that namespace in the
same process (`settle`); the other processes see a graph projection in flight
through `server/memory/app/graph_write_marks.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from server.core.background import register_deferred_task

log = logging.getLogger("server.memory.app.after_write")

_TASKS: set[asyncio.Task[Any]] = set()
_SCOPES: dict[asyncio.Task[Any], tuple[str, str]] = {}


def schedule(
    coro: Coroutine[Any, Any, Any], *, name: str, scope: tuple[str, str] | None = None
) -> asyncio.Task[Any] | None:
    """Run `coro` after the response; None (and the coroutine awaited by
    nobody is closed) when no loop is running. `scope` is the (org, namespace)
    the task writes, for `settle`."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    # Only live tasks of this loop stay in the set (a task of a
    # closed loop can never complete or be discarded).
    for stale in [t for t in _TASKS if t.done() or t.get_loop() is not loop]:
        _TASKS.discard(stale)
        _SCOPES.pop(stale, None)
    task = register_deferred_task(loop.create_task(coro, name=name))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    if scope is not None:
        _SCOPES[task] = scope
        task.add_done_callback(_forget_scope)
    task.add_done_callback(_note_failure)
    return task


def index_late_rows(rows: list[dict[str, Any]], *, ch: Any) -> asyncio.Task[Any] | None:
    """Fold a write's late embedding rows into the BM25 statistics after the response
    (`server/memory/store/bm25_stats.py`). A row is late when its write was slow (a long
    embed or judge, slow inserts), so the write does not also wait on these inserts; a
    miss only skews BM25 weights. No task for a write with no late row."""
    import server.memory.store as memory_repo

    late = memory_repo.late_rows(rows)
    if not late:
        return None
    return schedule(memory_repo.index_late_rows(late, ch=ch), name="loom-bm25-late-rows")


def _forget_scope(task: asyncio.Task[Any]) -> None:
    _SCOPES.pop(task, None)


def _note_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("after-write task %s failed: %s", task.get_name(), task.exception())


def pending() -> int:
    return sum(1 for t in _TASKS if not t.done())


async def drain(timeout_s: float = 10.0) -> int:
    """Wait for the side effects still being written (at most `timeout_s`);
    returns how many were in flight. Shutdown calls it before the client
    disconnects; tests call it to observe the rows."""
    live = [t for t in _TASKS if not t.done()]
    if live:
        await asyncio.wait(live, timeout=timeout_s)
    return len(live)


async def settle(org: str, namespace: str, *, timeout_s: float) -> int:
    """Wait (at most `timeout_s`) for this loop's live tasks scoped to
    (org, namespace); returns how many were in flight. Never raises: a task's
    own failure is its warning, and a timeout leaves the read to what landed."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    live = [
        t
        for t, sc in list(_SCOPES.items())
        if sc == (org, namespace) and not t.done() and t.get_loop() is loop
    ]
    if live and timeout_s > 0:
        await asyncio.wait(live, timeout=timeout_s)
    return len(live)


__all__ = ["drain", "index_late_rows", "pending", "schedule", "settle"]
