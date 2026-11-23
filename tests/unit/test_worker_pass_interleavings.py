"""Concurrent worker passes retain their own stop signal and cursor."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from server.workers import pass_context

Outcome = Literal["finished", "stopped", "cancelled"]
OUTCOMES: tuple[Outcome, ...] = ("finished", "stopped", "cancelled")


async def _worker_pass(
    name: str, ready: asyncio.Future[pass_context.PassContext], resume: asyncio.Event
) -> str:
    try:
        with pass_context.ensure_pass(name) as context:
            pass_context.set_cursor(name)
            ready.set_result(context)
            await resume.wait()
            assert pass_context.current() is context
            pass_context.check_stop()
            return context.cursor
    finally:
        assert pass_context.current() is None


async def _interleave(order: Sequence[int], outcomes: Sequence[Outcome]) -> None:
    loop = asyncio.get_running_loop()
    names = [f"worker-{index}" for index in range(len(outcomes))]
    ready: list[asyncio.Future[pass_context.PassContext]] = [loop.create_future() for _ in outcomes]
    resume = [asyncio.Event() for _ in outcomes]
    tasks = [
        asyncio.create_task(_worker_pass(name, entered, gate))
        for name, entered, gate in zip(names, ready, resume, strict=True)
    ]
    try:
        async with asyncio.timeout(5):
            contexts = await asyncio.gather(*ready)
            for index in order:
                task = tasks[index]
                if outcomes[index] == "cancelled":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                elif outcomes[index] == "stopped":
                    contexts[index].stopped.set()
                    resume[index].set()
                    with pytest.raises(pass_context.PassStopped):
                        await task
                else:
                    resume[index].set()
                    assert await task == names[index]
            assert [context.worker for context in contexts] == names
            assert [context.cursor for context in contexts] == names
            assert pass_context.current() is None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@settings(max_examples=36, derandomize=True, deadline=None)
@given(order=st.permutations(range(len(OUTCOMES))), outcomes=st.permutations(OUTCOMES))
def test_overlapping_worker_passes_keep_their_own_state(
    order: list[int], outcomes: list[Outcome]
) -> None:
    asyncio.run(_interleave(order, outcomes), debug=True)
