"""`to_its_end`: a write its caller waits for to the end, however often it is cancelled."""

from __future__ import annotations

import asyncio

import anyio
import pytest

from server.core import shielded as shielded_module
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


async def test_a_write_that_fails_after_a_cancel_raises_the_cancel() -> None:
    gate = asyncio.Event()

    async def write() -> None:
        await gate.wait()
        raise ValueError("the take-back failed")

    caller = asyncio.create_task(to_its_end(write(), name="t"))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert isinstance(raised.value.__cause__, ValueError)


def test_an_anyio_cancel_waits_for_the_end_without_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # anyio delivers a scope's cancel again at every await; the wait must not wake each time.
    waits = 0
    real_wait, real_shield = asyncio.wait, asyncio.shield

    async def counted_wait(*args: object, **kwargs: object) -> object:
        nonlocal waits
        waits += 1
        return await real_wait(*args, **kwargs)  # type: ignore[arg-type]

    def counted_shield(task: object) -> object:
        nonlocal waits
        waits += 1
        return real_shield(task)  # type: ignore[arg-type]

    monkeypatch.setattr(shielded_module.asyncio, "wait", counted_wait)
    monkeypatch.setattr(shielded_module.asyncio, "shield", counted_shield)
    finished: list[bool] = []

    async def write() -> None:
        await asyncio.sleep(0.1)
        finished.append(True)

    async def main() -> None:
        async with anyio.create_task_group() as group:

            async def caller() -> None:
                with pytest.raises(asyncio.CancelledError):
                    await to_its_end(write(), name="t")
                assert finished == [True]

            group.start_soon(caller)
            await anyio.sleep(0.01)
            group.cancel_scope.cancel()

    anyio.run(main)
    assert finished == [True] and waits <= 3
