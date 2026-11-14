"""`to_its_end`: a write its caller waits for to the end, however often it is cancelled."""

from __future__ import annotations

import asyncio

import pytest

from server.core.shielded import to_its_end


async def test_it_answers_what_the_write_returns() -> None:
    async def write() -> int:
        return 7

    assert await to_its_end(write(), name="t") == 7


async def test_repeated_cancels_wait_for_the_end_then_raise() -> None:
    gate = asyncio.Event()
    done: list[bool] = []

    async def write() -> None:
        await gate.wait()
        done.append(True)

    caller = asyncio.create_task(to_its_end(write(), name="t"))
    await asyncio.sleep(0)
    for _ in range(3):
        caller.cancel()
        await asyncio.sleep(0)
    assert not caller.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert done == [True]


async def test_a_failing_write_raises_its_error() -> None:
    async def write() -> None:
        raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        await to_its_end(write(), name="t")
