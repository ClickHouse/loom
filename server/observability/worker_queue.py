"""A bounded queue drained by one detached worker task, both created lazily on
the running loop.

An `asyncio.Queue` binds to the loop that first awaits it, so a queue built on
one loop can never be drained on another (a test calling `asyncio.run`, a CLI,
a restarted lifespan). `ensure()` rebuilds the queue and the worker when the
loop changes, and restarts a worker that died; a `get()` that fails (a queue
unusable on this loop) stands the worker down and drops the queue so the next
`ensure()` rebuilds both. Started on first use, not in the lifespan, so
importing a module that owns one spawns nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)


class LoopBoundQueue:
    def __init__(
        self, *, name: str, maxsize: int, handle: Callable[[Any], Awaitable[None]]
    ) -> None:
        self.name = name
        self.maxsize = maxsize
        self._handle = handle
        self.queue: asyncio.Queue[Any] | None = None
        self.worker: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def ensure(self) -> asyncio.Queue[Any] | None:
        """The queue on the running loop, with its worker running; None when
        there is no running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._loop is not None and self._loop is not loop:
            self.reset()
        self._loop = loop
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=self.maxsize)
        if self.worker is None or self.worker.done():
            # Detached: a worker that inherited the first caller's context
            # would tag, trace and bill every later item as that request.
            from server.core.background import detached_task

            self.worker = detached_task(self._run(), name=self.name)
        return self.queue

    def reset(self) -> None:
        """Drop the queue and worker handle so the next `ensure()` rebuilds both."""
        self.queue = None
        self.worker = None

    def running(self) -> bool:
        return self.worker is not None and not self.worker.done()

    def qsize(self) -> int:
        return self.queue.qsize() if self.queue is not None else 0

    async def stop_worker(self) -> None:
        """Cancel the worker and wait for it to end."""
        worker = self.worker
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def _run(self) -> None:
        assert self.queue is not None
        q = self.queue
        while True:
            try:
                item = await q.get()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — an unusable queue stands the worker down
                log.warning(
                    "%s queue is unusable on this loop; the next enqueue rebuilds it", self.name
                )
                self.reset()
                return
            try:
                await self._handle(item)
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad item never stops the worker
                log.exception("%s worker iteration failed", self.name)
            finally:
                with contextlib.suppress(Exception):
                    q.task_done()
