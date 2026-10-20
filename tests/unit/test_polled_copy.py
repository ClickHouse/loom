"""PolledCopy: when a copy is read, who waits for the read, and what is served."""

from __future__ import annotations

import asyncio
import logging

import pytest

from server.core import polled_copy
from server.core.polled_copy import PolledCopy
from tests.unit.clock_fake import ManualClock

_LOG = logging.getLogger(__name__)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    c = ManualClock(t=1000.0)
    monkeypatch.setattr(polled_copy.time, "monotonic", c.monotonic)
    return c


async def test_a_read_slower_than_the_refresh_is_not_repeated_by_each_waiter(
    clock: ManualClock,
) -> None:
    """A change here asked for a read, three calls arrive for it, two queue behind the one that
    reads, and the read fails after longer than `refresh_s`: the next read is counted from its
    end, so the waiters behind it are served the last copy instead of each reading."""
    reads = 0

    async def failing() -> str:
        nonlocal reads
        reads += 1
        clock.advance(0.05)
        await asyncio.sleep(0)  # the other calls queue on the lock meanwhile
        raise ConnectionError("clickhouse away")

    copy: PolledCopy[str] = PolledCopy("t", failing, _LOG)
    copy._reset_for_tests("last", read_age_s=0.0, due_in_s=10)
    copy.note_change()  # what makes the read due
    got = await asyncio.gather(*(copy.get(refresh_s=0.01, max_stale_s=60) for _ in range(3)))
    assert got == ["last"] * 3
    assert reads == 1


async def test_the_copy_is_aged_from_the_start_of_its_read(clock: ManualClock) -> None:
    """A read that takes 0.05 s leaves a copy already 0.05 s old: with a 0.04 s
    bound it is not served."""

    async def slow() -> str:
        clock.advance(0.05)
        return "new"

    copy: PolledCopy[str] = PolledCopy("t", slow, _LOG)
    assert await copy.get(refresh_s=10, max_stale_s=0.04) is None


async def test_a_change_made_here_is_waited_for_even_with_a_young_copy(
    clock: ManualClock,
) -> None:
    """After `note_change`, a call made while a read runs waits for it: the read in
    progress may be the one that sees the change."""
    reading, release = asyncio.Event(), asyncio.Event()

    async def read() -> str:
        reading.set()
        await release.wait()
        return "changed"

    copy: PolledCopy[str] = PolledCopy("t", read, _LOG)
    copy._reset_for_tests("old", read_age_s=0.0, due_in_s=10)
    copy.note_change()
    first = asyncio.create_task(copy.get(refresh_s=10, max_stale_s=60))
    await reading.wait()
    second = asyncio.create_task(copy.get(refresh_s=10, max_stale_s=60))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    assert await first == "changed"
    assert await second == "changed"


async def test_a_failed_read_logs_on_the_callers_logger(caplog, clock: ManualClock) -> None:
    async def failing() -> str:
        raise ConnectionError("clickhouse away")

    owner = logging.getLogger("server.llm.customer")
    copy: PolledCopy[str] = PolledCopy("model-call switch", failing, owner)
    with caplog.at_level(logging.WARNING, logger="server.llm.customer"):
        assert await copy.get(refresh_s=10, max_stale_s=60) is None
    assert [r.name for r in caplog.records] == ["server.llm.customer"]
    assert "model-call switch: read failed" in caplog.records[0].getMessage()


async def test_a_cancelled_read_hands_over_to_the_first_queued_call(clock: ManualClock) -> None:
    """The call that was reading goes away; the calls queued behind it do not
    find a copy nobody read: the first one reads, the next is served it."""
    reading = asyncio.Event()
    reads = 0

    async def read() -> str:
        nonlocal reads
        reads += 1
        if reads == 1:
            reading.set()
            await asyncio.Event().wait()  # until cancelled
        return "fresh"

    copy: PolledCopy[str] = PolledCopy("t", read, _LOG)
    first = asyncio.create_task(copy.get(refresh_s=10, max_stale_s=60))
    await reading.wait()
    queued = [asyncio.create_task(copy.get(refresh_s=10, max_stale_s=60)) for _ in range(2)]
    await asyncio.sleep(0)
    first.cancel()
    assert await asyncio.gather(*queued) == ["fresh", "fresh"]
    assert reads == 2
