"""flush_now waits for a batch the timer already popped and is still inserting.

The history-policy gate calls `flush_now` before it reads
`agent_events` for the caller's immediately preceding call. When the 100 ms
timer has already taken that event off the queue and is awaiting the INSERT,
the queue is empty; returning at once would let the gate read the trace
before the witness row lands and deny a set that followed a read. So
`flush_now` also waits for the in-flight batch.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from server.observability import events as ev


class _SlowClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.written: list[list[dict[str, Any]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.started.set()
        await self.release.wait()
        self.written.append(rows)


def _event() -> ev.AgentEvent:
    return ev.AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        operation="memory.get",
        request_id="r",
        latency_ms=1,
        success=True,
    )


@pytest.mark.asyncio
async def test_flush_now_returns_only_after_the_inflight_batch_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    client = _SlowClient()
    monkeypatch.setattr(ev, "get_client", lambda: client)
    em = ev.EventEmitter()
    em.emit(_event())

    # The timer's flush: pops the batch and blocks inside the INSERT.
    async def _timer_flush() -> None:
        await em._flush_once()

    timer = asyncio.get_running_loop().create_task(_timer_flush())
    await client.started.wait()
    assert em.queue.empty()  # the row is off the queue and mid-flight

    waiter = asyncio.get_running_loop().create_task(em.flush_now())
    await asyncio.sleep(0.02)
    assert (
        not waiter.done()
    )  # flush_now is waiting for the in-flight batch, not returning on the empty queue

    client.release.set()
    await asyncio.wait_for(timer, 1.0)
    await asyncio.wait_for(waiter, 1.0)
    assert len(client.written) == 1 and client.written[0][0]["operation"] == "memory.get"


@pytest.mark.asyncio
async def test_flush_now_still_drains_a_quiet_queue(monkeypatch: pytest.MonkeyPatch) -> None:

    client = _SlowClient()
    client.release.set()
    monkeypatch.setattr(ev, "get_client", lambda: client)
    em = ev.EventEmitter()
    em.emit(_event())
    em.emit(_event())
    await asyncio.wait_for(em.flush_now(), 1.0)
    assert sum(len(b) for b in client.written) == 2 and em.queue.empty()


@pytest.mark.asyncio
async def test_lost_events_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        async def insert(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("down")

    monkeypatch.setattr(ev, "get_client", lambda: _Boom())
    em = ev.EventEmitter(queue=asyncio.Queue(maxsize=2))
    for _ in range(5):
        em.emit(_event())
    assert em.stats() == {
        "queued": 2,
        "dropped_queue_full": 3,
        "dropped_flush_failed": 0,
        "dropped_batch_repeat": 0,
        "batch_repeats": 0,
    }
    await em.flush_now()
    assert em.stats() == {
        "queued": 0,
        "dropped_queue_full": 3,
        "dropped_flush_failed": 2,
        "dropped_batch_repeat": 0,
        "batch_repeats": 0,
    }


@pytest.mark.asyncio
async def test_a_down_store_logs_the_failure_without_a_line_per_batch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Boom:
        async def insert(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("down")

    monkeypatch.setattr(ev, "get_client", lambda: _Boom())
    em = ev.EventEmitter()
    with caplog.at_level("WARNING", logger=ev.log.name):
        for _ in range(12):
            em.emit(_event())
            await em.flush_now()
    lines = [r for r in caplog.records if "agent_events flush failed" in r.getMessage()]
    assert len(lines) == 2  # the 1st and the 10th failed batch
    assert lines[0].exc_info and not lines[1].exc_info
    assert em.stats()["dropped_flush_failed"] == 12
