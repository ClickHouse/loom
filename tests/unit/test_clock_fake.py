"""The manual clock moves only when told and records every sleep."""

from __future__ import annotations

import asyncio

import pytest

from tests.unit.clock_fake import ManualClock


def test_every_reading_is_the_same_time() -> None:
    clock = ManualClock(t=5.0)
    assert clock() == clock.monotonic() == clock.time() == 5.0


def test_sleeps_advance_and_are_recorded() -> None:
    clock = ManualClock()
    clock.sleep(1.5)
    clock.sleep(-1)  # a negative sleep returns at once, as time.sleep would refuse to wait
    clock.advance(0.5)
    assert clock.t == 2.0 and clock.sleeps == [1.5, -1]


def test_time_never_runs_backwards() -> None:
    with pytest.raises(ValueError):
        ManualClock().advance(-0.1)


async def test_async_sleep_lets_other_tasks_run_without_waiting() -> None:
    clock = ManualClock()
    ran: list[str] = []

    async def other() -> None:
        ran.append("other")

    task = asyncio.create_task(other())
    await clock.async_sleep(30)
    assert ran == ["other"] and clock.t == 30
    await task
