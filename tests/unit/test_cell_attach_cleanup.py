"""The attach owner preserves failures through bounded public-close cleanup."""

import argparse
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from server.cli import cell_attach, cell_control
from tests.unit.cell_control_owner import owner
from tests.unit.test_cell_control import snapshot


@pytest.mark.asyncio
async def test_failed_work_keeps_its_error_when_public_close_also_fails():
    events = []

    class Conn:
        async def close(self, **kwargs):
            events.append("close")
            raise RuntimeError("close failed")

        def terminate(self):
            events.append("abort")

    original = ValueError("original work failure")

    async def work():
        raise original

    with pytest.raises(ValueError, match="original work failure") as caught:
        await owner(Conn()).call(work, total_s=0.1)
    assert caught.value is original
    assert events[0] == "close" and events.count("close") == 1
    assert events[1:] and set(events[1:]) == {"abort"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_second_cancellation_preserves_original_failure_and_closes_before_cancel(failed):
    entered, closing = asyncio.Event(), asyncio.Event()
    events = []

    class Conn:
        async def close(self, **kwargs):
            events.append("close")
            closing.set()
            await asyncio.Future()

        def terminate(self):
            events.append("abort")

        def is_closed(self):
            return False

    async def work():
        entered.set()
        try:
            if failed:
                raise ValueError("original work failure")
            await asyncio.Future()
        finally:
            events.append("work ended")

    owned = owner(Conn())
    task = asyncio.create_task(owned.call(work, total_s=0.2))
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        if not failed:
            task.cancel("first cancellation")
        await asyncio.wait_for(closing.wait(), timeout=0.5)
        task.cancel("second cancellation")
        with pytest.raises(
            ValueError if failed else asyncio.CancelledError,
            match="original work failure" if failed else "first cancellation",
        ):
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if not done:
                raise TimeoutError("owned test task did not finish")
            await task
        assert events.count("abort") >= 1
        if not failed:
            assert events.index("close") < events.index("work ended")
    except TimeoutError:
        if task.done():
            task.result()
        raise
    finally:
        owned_tasks = {task, *owned._tasks}
        if owned._close_task is not None:
            owned_tasks.add(owned._close_task)
        for child in owned_tasks:
            if not child.done():
                child.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_lock_owner_bounds_close_inside_the_same_cleanup_allowance():
    closing = asyncio.Event()
    conn = AsyncMock()
    conn.terminate = lambda: None

    async def close(**_):
        closing.set()
        await asyncio.Future()

    conn.close.side_effect = close
    owned = cell_control.Owner(exit_s=0.03)
    owned.conn = conn
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await owned.close()
    assert time.monotonic() - started < 0.15
    assert closing.is_set() and owned.closed
    conn.close.assert_awaited_once()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_run_preserves_its_own_failure_without_hiding_normal_close_failure(
    failed, monkeypatch
):
    router = AsyncMock()
    router.call.return_value = {**snapshot(), "state": "attaching"}
    run = cell_attach.Run(
        None, argparse.Namespace(cell="test", wait=0, dry_run=True), router, lambda _: None
    )
    original = ValueError("run failed")
    conn = AsyncMock()
    conn.terminate = lambda: None
    conn.close.side_effect = RuntimeError("close failed")

    async def open(owned, what):
        owned.conn = conn
        if failed:
            raise original

    monkeypatch.setattr(cell_control.Owner, "_open", open)
    run.read_own_row = AsyncMock()
    run.refusals = AsyncMock(return_value=(None, None, None))
    run.dry_run = AsyncMock(return_value=0)
    try:
        raise LookupError("unrelated caller exception")
    except LookupError:
        with pytest.raises(
            ValueError if failed else RuntimeError, match="run failed" if failed else "close failed"
        ) as caught:
            await run.run()
        if failed:
            assert caught.value is original
    conn.close.assert_awaited_once()
