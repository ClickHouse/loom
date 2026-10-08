"""A cancelled code.search / code.grep request takes its per-namespace
searches and its legs with it: `asyncio.wait` leaves the tasks it waits on
running when the waiter is cancelled, and a gather leaves siblings running
when one leg raises."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app import search_fanout as fanout
from server.code.app import search_legs as legs_mod


class _Inp:
    def model_copy(self, update: dict[str, Any]) -> _Inp:
        return self


async def _cancel_after_start(coro: Any, started: asyncio.Event) -> None:
    task = asyncio.ensure_future(coro)
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_cancelled_fanout_cancels_every_namespace_search() -> None:
    started, cancelled = asyncio.Event(), []

    async def single(sub: Any) -> Any:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    await _cancel_after_start(
        fanout.run_all(_Inp(), ["a", "b"], single, timeout_ms=30_000), started
    )
    await asyncio.sleep(0)
    assert len(cancelled) == 2


@pytest.mark.asyncio
async def test_a_cancelled_or_failed_leg_run_leaves_no_leg_running(monkeypatch) -> None:
    monkeypatch.setattr(legs_mod.settings, "code_search_llm_leg_grace_ms", 0)
    started, cancelled = asyncio.Event(), []

    async def slow() -> list[Any]:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return []

    result = SimpleNamespace(legs_timed_out=[], epistemic="", budget=None)
    await _cancel_after_start(
        legs_mod.run_legs(
            {"lexical": (slow(), []), "vector": (slow(), [])},
            result=result,
            work={},
            deadline_ms=30_000,
        ),
        started,
    )
    await asyncio.sleep(0)
    assert len(cancelled) == 2

    async def boom() -> list[Any]:
        raise RuntimeError("leg failed")

    cancelled.clear()
    started.clear()
    with pytest.raises(RuntimeError):
        await legs_mod.run_legs(
            {"lexical": (boom(), []), "vector": (slow(), [])}, result=result, work={}
        )
    await asyncio.sleep(0)
    assert cancelled == [1]  # the unbounded gather's sibling does not keep reading
