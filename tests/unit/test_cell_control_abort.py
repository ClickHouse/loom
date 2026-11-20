"""A failed fallback abort cannot replace the native owner's first failure."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from server.cli import cell_control

pytestmark = pytest.mark.asyncio
ERRORS = (ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt)
ABORT_ERRORS = (RuntimeError, asyncio.CancelledError, SystemExit, KeyboardInterrupt)


async def outcome(work):
    try:
        result = await cell_control._captured(work)
    except BaseException as error:  # noqa: BLE001 — observe the identical native failure inertly.
        return error
    assert isinstance(result, cell_control._Fatal), "a failed required close cannot succeed"
    return result.error


@pytest.mark.parametrize("first_kind", ERRORS)
@pytest.mark.parametrize("abort_kind", ABORT_ERRORS)
async def test_direct_owner_keeps_first_close_failure_and_retrieves_child(
    monkeypatch, first_kind, abort_kind
):
    first, secondary = first_kind("first close failure"), abort_kind("abort failure")
    aborted = []

    class Connection:
        def terminate(self):
            aborted.append(True)
            raise secondary

    owner = cell_control.Owner(exit_s=0.2)
    owner.conn = Connection()
    close = AsyncMock(side_effect=first)
    monkeypatch.setattr(cell_control.pg, "close_owned", close)
    result = await outcome(owner.close)
    assert result is first
    assert aborted == [True]
    close.assert_awaited_once()
    assert owner.closed and owner._close_task.done()
    assert owner._close_task.cancelled() or not owner._close_task._log_traceback


@pytest.mark.parametrize("abort_kind", ABORT_ERRORS)
@pytest.mark.parametrize("first_kind", ERRORS)
async def test_outer_signal_wins_over_later_close_and_abort_failures(
    monkeypatch, first_kind, abort_kind
):
    entered, finish = asyncio.Event(), asyncio.Event()
    first, secondary = first_kind("close failed after signal"), abort_kind("abort failure")
    seen = []
    actual_wait = asyncio.wait
    owner = cell_control.Owner(exit_s=0.2)

    class Connection:
        def terminate(self):
            raise secondary

    async def close(conn, *, end):
        entered.set()
        await finish.wait()
        raise first

    async def wait(*args, **kwargs):
        try:
            return await actual_wait(*args, **kwargs)
        except asyncio.CancelledError as error:
            seen.append(error)
            raise

    owner.conn = Connection()
    monkeypatch.setattr(cell_control.pg, "close_owned", close)
    monkeypatch.setattr(cell_control.asyncio, "wait", wait)
    task = asyncio.create_task(outcome(owner.close))
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        original_end = owner.close_end
        task.cancel("original outer cancellation")
        await asyncio.sleep(0)
        assert len(seen) == 1 and not task.done()
        finish.set()
        done, _ = await asyncio.wait({task}, timeout=0.5)
        if not done:
            raise TimeoutError("owned test task did not finish")
        result = await task
        assert result is seen[0]
        assert owner.close_end == original_end
        assert owner._close_task.done()
        assert owner._close_task.cancelled() or not owner._close_task._log_traceback
    except TimeoutError:
        if task.done():
            task.result()
        raise
    finally:
        finish.set()
        owned_tasks = {task, *owner._tasks}
        if owner._close_task is not None:
            owned_tasks.add(owner._close_task)
        for child in owned_tasks:
            if not child.done():
                child.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)


@pytest.mark.parametrize("abort_kind", ABORT_ERRORS)
async def test_abort_failure_still_cancels_and_retrieves_expired_close_child(
    monkeypatch, abort_kind
):
    entered, ended = asyncio.Event(), asyncio.Event()
    secondary = abort_kind("abort failure")

    class Connection:
        def terminate(self):
            raise secondary

    async def close(conn, *, end):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            ended.set()
            raise ValueError("late close failure")

    owner = cell_control.Owner(exit_s=0.02)
    owner.conn = Connection()
    monkeypatch.setattr(cell_control.pg, "close_owned", close)
    started = time.monotonic()
    result = await outcome(owner.close)
    assert isinstance(result, TimeoutError)
    assert time.monotonic() - started < 0.2
    assert entered.is_set()
    await asyncio.wait_for(ended.wait(), 0.2)
    await asyncio.sleep(0)
    assert owner._close_task.done() and not owner._close_task._log_traceback


@pytest.mark.parametrize("abort_kind", ABORT_ERRORS)
async def test_self_cancelled_close_is_terminal_even_when_abort_fails(monkeypatch, abort_kind):
    first = asyncio.CancelledError("close cancelled itself")
    calls = []

    class Connection:
        def terminate(self):
            calls.append("abort")
            raise abort_kind("abort failure")

    async def close(conn, *, end):
        calls.append("close")
        raise first

    owner = cell_control.Owner(exit_s=60)
    owner.conn = Connection()
    monkeypatch.setattr(cell_control.pg, "close_owned", close)
    started = time.monotonic()
    result = await outcome(owner.close)
    assert result is first
    assert time.monotonic() - started < 0.2
    assert calls == ["close", "abort"]


@pytest.mark.parametrize("wait_kind", ERRORS)
@pytest.mark.parametrize("abort_kind", ABORT_ERRORS)
async def test_failed_reap_abort_cannot_replace_the_callers_first_failure(
    monkeypatch, wait_kind, abort_kind
):
    owner = cell_control.Owner()
    owner.close_end = time.monotonic() + 0.2
    calls = []

    class Connection:
        def terminate(self):
            calls.append("abort")
            raise abort_kind("abort failed while reaping")

    async def failed_wait(*args, **kwargs):
        raise wait_kind("reap wait interrupted")

    owner.conn = Connection()
    child = owner._start(lambda: asyncio.Future())
    monkeypatch.setattr(cell_control.asyncio, "wait", failed_wait)
    try:
        try:
            result = await cell_control._captured(lambda: owner._stop({child}))
        except BaseException as error:  # noqa: BLE001 — the probe must expose secondary native errors.
            result = error
        assert result is None, "reaping cannot replace an earlier operation failure"
        assert calls == ["abort"]
    finally:
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
