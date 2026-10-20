"""PolledCopy: when a copy is read, who waits for the read, and what is served."""

from __future__ import annotations

import asyncio
import logging
import time

from server.core.polled_copy import PolledCopy

_LOG = logging.getLogger(__name__)


async def test_a_read_slower_than_the_refresh_is_not_repeated_by_each_waiter() -> None:
    """A change here asked for a read, three calls queue for it, and the read
    fails after longer than `refresh_s`: the next read is counted from its end,
    so the waiters behind it are served the last copy instead of each reading."""
    reads = 0

    async def failing() -> str:
        nonlocal reads
        reads += 1
        await asyncio.sleep(0.05)
        raise ConnectionError("clickhouse away")

    copy = PolledCopy("t", "", failing, _LOG)
    copy._reset_for_tests("last", read=True, refresh_s=0.01)
    copy.forget()
    got = await asyncio.gather(*(copy.get(refresh_s=0.01, max_stale_s=60) for _ in range(3)))
    assert got == ["last"] * 3
    assert reads == 1


async def test_the_copy_is_aged_from_the_start_of_its_read() -> None:
    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "new"

    copy = PolledCopy("t", "", slow, _LOG)
    before = time.monotonic()
    assert await copy.get(refresh_s=10, max_stale_s=60) == "new"
    assert before <= copy._read_at < before + 0.04


async def test_a_change_made_here_is_waited_for_even_with_a_young_copy() -> None:
    """After `forget`, a call made while a read runs waits for it: the read in
    progress may be the one that sees the change."""
    reading, release = asyncio.Event(), asyncio.Event()

    async def read() -> str:
        reading.set()
        await release.wait()
        return "changed"

    copy = PolledCopy("t", "", read, _LOG)
    copy._reset_for_tests("old", read=True, refresh_s=10)
    copy.forget()
    first = asyncio.ensure_future(copy.get(refresh_s=10, max_stale_s=60))
    await reading.wait()
    second = asyncio.ensure_future(copy.get(refresh_s=10, max_stale_s=60))
    await asyncio.sleep(0.02)
    assert not second.done()
    release.set()
    assert await first == "changed"
    assert await second == "changed"


async def test_a_failed_read_logs_on_the_callers_logger(caplog) -> None:
    async def failing() -> str:
        raise ConnectionError("clickhouse away")

    owner = logging.getLogger("server.llm.customer")
    copy = PolledCopy("model-call switch", "", failing, owner)
    with caplog.at_level(logging.WARNING, logger="server.llm.customer"):
        assert await copy.get(refresh_s=10, max_stale_s=60) is None
    assert [r.name for r in caplog.records] == ["server.llm.customer"]
    assert "model-call switch: read failed" in caplog.records[0].getMessage()
