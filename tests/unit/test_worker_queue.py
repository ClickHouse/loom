"""The loop-bound queue behind the Langfuse score poster and the retrieval
judge: a second event loop rebuilds the queue and its worker, a worker that
died restarts on the next enqueue, and one failing item never stops it."""

from __future__ import annotations

import asyncio
from typing import Any

from server.observability.worker_queue import LoopBoundQueue


def test_a_second_loop_rebuilds_the_queue_and_the_worker() -> None:
    seen: list[Any] = []

    async def _handle(item: Any) -> None:
        seen.append(item)

    lq = LoopBoundQueue(name="t", maxsize=10, handle=_handle)
    assert lq.ensure() is None  # no running loop

    async def _one(item: Any) -> asyncio.Queue[Any]:
        q = lq.ensure()
        assert q is not None
        q.put_nowait(item)
        await asyncio.wait_for(q.join(), timeout=5)
        return q

    first = asyncio.run(_one("a"))
    second = asyncio.run(_one("b"))
    assert first is not second and seen == ["a", "b"]


def test_a_dead_worker_restarts_and_a_failing_item_is_skipped() -> None:
    seen: list[Any] = []

    async def _handle(item: Any) -> None:
        if item == "bad":
            raise RuntimeError("boom")
        seen.append(item)

    lq = LoopBoundQueue(name="t", maxsize=10, handle=_handle)

    async def _go() -> None:
        q = lq.ensure()
        assert q is not None
        q.put_nowait("bad")
        q.put_nowait("ok")
        await asyncio.wait_for(q.join(), timeout=5)
        assert lq.running()
        await lq.stop_worker()
        assert not lq.running()
        assert lq.ensure() is q and lq.running()  # same loop: same queue, new worker
        q.put_nowait("again")
        await asyncio.wait_for(q.join(), timeout=5)
        await lq.stop_worker()

    asyncio.run(_go())
    assert seen == ["ok", "again"]
