"""`TaskSet.spawn` holds a fire-and-forget task until it is done, and a
detached spawn starts with an empty context."""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from server.core.background import TaskSet

_tag: contextvars.ContextVar[str] = contextvars.ContextVar("tag", default="")


@pytest.mark.asyncio
async def test_tasks_are_held_until_done_and_iterable_for_a_drain() -> None:
    ts = TaskSet()
    gate = asyncio.Event()

    async def _wait() -> int:
        await gate.wait()
        return 1

    t = ts.spawn(_wait(), name="t")
    assert len(ts) == 1 and list(ts) == [t]
    gate.set()
    assert await asyncio.gather(*ts) == [1]
    await asyncio.sleep(0)
    assert len(ts) == 0 and not ts


@pytest.mark.asyncio
async def test_detached_spawn_does_not_inherit_the_callers_context() -> None:
    ts = TaskSet()
    _tag.set("request-1")

    async def _read() -> str:
        return _tag.get()

    assert await ts.spawn(_read(), name="inherits") == "request-1"
    assert await ts.spawn(_read(), name="detached", detached=True) == ""


def test_spawn_without_a_loop_raises_and_holds_nothing() -> None:
    ts = TaskSet()

    async def _noop() -> None:
        return None

    coro = _noop()
    with pytest.raises(RuntimeError):
        ts.spawn(coro, name="x")
    coro.close()
    assert len(ts) == 0
