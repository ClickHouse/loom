"""Placement reuses the control owner without replacing its phase budgets."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.cli import cell_control
from server.core import pg
from tests.unit.clock_fake import ManualClock
from tests.unit.test_cell_attach import FakeConn

pytestmark = pytest.mark.asyncio


async def test_custom_exit_is_used_only_outside_a_local_phase():
    owner = cell_control.Owner(exit_s=0.09)
    conn = FakeConn()

    async def opening():
        owner.conn = conn

    before = time.monotonic()
    assert await owner.run(opening, AsyncMock(return_value=17), opening_s=0.3) == 17
    assert before + 0.09 <= owner.close_end <= time.monotonic() + 0.09
    assert conn.executed == ["CLOSE"]


@pytest.mark.parametrize("occupied", ["conn", "phase", "task", "closed", "repeated"])
async def test_command_entry_refuses_reuse_before_any_callback(occupied):
    owner = cell_control.Owner()
    held = None
    if occupied == "conn":
        owner.conn = FakeConn()
    elif occupied == "phase":
        owner.phase_end = time.monotonic() + 1
    elif occupied == "task":
        held = owner._start(lambda: asyncio.Future())
    elif occupied == "closed":
        await owner.close()
    else:
        await owner.run(AsyncMock(), AsyncMock(), opening_s=0.1)
    opening, body = AsyncMock(), AsyncMock()
    try:
        with pytest.raises(cell_control.Refused, match="fresh"):
            await owner.run(opening, body, opening_s=0.1)
        opening.assert_not_called()
        body.assert_not_called()
    finally:
        if held is not None:
            held.cancel()
            await asyncio.gather(held, return_exceptions=True)


@pytest.mark.parametrize("stage", ["opening", "body"])
async def test_recursive_entry_cannot_replace_the_original_backend(stage):
    owner = cell_control.Owner()
    original = FakeConn()
    other_open, other_body = AsyncMock(), AsyncMock()

    async def recurse():
        with pytest.raises(cell_control.Refused, match="fresh"):
            await owner.run(other_open, other_body, opening_s=0.1)

    async def opening():
        if stage == "opening":
            await recurse()
        owner.conn = original

    async def body():
        if stage == "body":
            await recurse()
        return 3

    assert await owner.run(opening, body, opening_s=0.1) == 3
    other_open.assert_not_called()
    other_body.assert_not_called()
    assert owner.conn is original and original.executed == ["CLOSE"]


async def test_native_wrapper_keeps_its_existing_three_budgets(monkeypatch):
    observed = {}
    actual_run = cell_control.Owner.run
    actual_call = cell_control.Owner.call

    async def wrapped_run(self, opening, work, *, opening_s):
        observed.update(opening=opening_s, exit=self.exit_s)
        return await actual_run(self, opening, work, opening_s=opening_s)

    async def wrapped_call(self, work, *, total_s):
        observed.setdefault("phases", []).append(total_s)
        return await actual_call(self, work, total_s=total_s)

    async def opening(self, what):
        assert what == "native"
        self.conn = FakeConn()

    monkeypatch.setattr(cell_control.Owner, "run", wrapped_run)
    monkeypatch.setattr(cell_control.Owner, "call", wrapped_call)
    monkeypatch.setattr(cell_control.Owner, "_open", opening)
    assert (
        await cell_control.run("native", lambda owner: owner.transaction(AsyncMock(return_value=7)))
        == 7
    )
    assert observed == {"opening": 75.0, "exit": pg._RELEASE_TIMEOUT_S, "phases": [75.0, 60.0]}


async def test_failed_phase_keeps_its_end_instead_of_custom_exit():
    owner = cell_control.Owner(exit_s=99)
    conn = FakeConn()

    async def opening():
        owner.conn = conn

    async def body():
        return await owner.call(lambda: asyncio.Future(), total_s=0.04)

    before = time.monotonic()
    with pytest.raises(TimeoutError):
        await owner.run(opening, body, opening_s=0.1)
    assert before < owner.close_end <= before + 0.1
    assert owner.close_end == owner.phase_end
    assert conn.executed == ["CLOSE"]


async def test_public_close_defers_repeated_signals_until_acknowledged_within_original_end():
    events = []
    closing, finish = asyncio.Event(), asyncio.Event()

    class Connection:
        async def close(self, **kwargs):
            events.append("close")
            closing.set()
            await finish.wait()
            events.append("ack")

        def terminate(self):
            events.append("abort")

        def is_closed(self):
            return "ack" in events

    owner = cell_control.Owner(exit_s=0.2)
    owner.conn = Connection()
    task = asyncio.create_task(owner.close())
    try:
        await asyncio.wait_for(closing.wait(), timeout=0.5)
        original_end = owner.close_end
        task.cancel("first signal")
        await asyncio.sleep(0)
        task.cancel("second signal")
        await asyncio.sleep(0)
        assert not task.done(), "a second signal must not interrupt owned public close"
        assert owner.close_end == original_end
        finish.set()
        with pytest.raises(asyncio.CancelledError, match="first signal"):
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if not done:
                raise TimeoutError("owned test task did not finish")
            await task
        assert events == ["close", "ack"]
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


async def test_repeated_signals_cannot_extend_an_expired_public_close(monkeypatch):
    clock = ManualClock(t=100.0)
    local_time = SimpleNamespace(monotonic=clock.monotonic)
    monkeypatch.setattr(cell_control, "time", local_time)
    monkeypatch.setattr(pg, "time", local_time)
    closing = asyncio.Event()
    conn = FakeConn()

    async def stalled(**kwargs):
        closing.set()
        await asyncio.Future()

    conn.close = stalled
    owner = cell_control.Owner(exit_s=0.04)
    owner.conn = conn
    waits = []

    async def signalled_wait(tasks, **kwargs):
        assert tasks == {owner._close_task}
        waits.append(kwargs["timeout"])
        assert len(waits) <= 2, "signals cannot grant another cleanup allowance"
        await closing.wait()
        if len(waits) == 1:
            clock.advance(0.01)
        else:
            clock.t = 100.04
        asyncio.current_task().cancel("first signal" if len(waits) == 1 else "second signal")
        await asyncio.sleep(0)
        raise AssertionError("cancellation must reach the public close")

    # Replace only this module's wait seam; native close and the event loop stay real.
    monkeypatch.setattr(
        cell_control,
        "asyncio",
        SimpleNamespace(
            create_task=asyncio.create_task,
            CancelledError=asyncio.CancelledError,
            wait=signalled_wait,
        ),
    )
    task = asyncio.create_task(owner.close())
    try:
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert done, "repeated signals cannot restart the original cleanup allowance"
        with pytest.raises(asyncio.CancelledError, match="first signal"):
            await task
        assert waits == pytest.approx([0.04, 0.03])
        assert clock.monotonic() - 100.0 == pytest.approx(0.04)
        assert owner.close_end == 100.04
        assert "TERMINATE" in conn.executed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if owner._close_task is not None:
            await asyncio.gather(owner._close_task, return_exceptions=True)


async def test_closed_transport_cannot_acknowledge_pending_public_cancellation():
    closing, acknowledged = asyncio.Event(), asyncio.Event()
    events = []

    class Connection:
        async def close(self, **kwargs):
            closing.set()
            await acknowledged.wait()
            events.append("ack")

        def is_closed(self):
            # An aborted transport is closed even while its cancellation is pending.
            return True

        def terminate(self):
            events.append("abort")

    owner = cell_control.Owner(exit_s=0.2)
    owner.conn = Connection()
    task = asyncio.create_task(owner.close())
    try:
        await asyncio.wait_for(closing.wait(), timeout=0.5)
        task.cancel("stop")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done() and events == []
        acknowledged.set()
        with pytest.raises(asyncio.CancelledError, match="stop"):
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if not done:
                raise TimeoutError("owned test task did not finish")
            await task
        assert events == ["ack"]
    except TimeoutError:
        if task.done():
            task.result()
        raise
    finally:
        acknowledged.set()
        owned_tasks = {task, *owner._tasks}
        if owner._close_task is not None:
            owned_tasks.add(owner._close_task)
        for child in owned_tasks:
            if not child.done():
                child.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)
