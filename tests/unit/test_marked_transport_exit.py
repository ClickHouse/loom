"""Cancelled marked operations retain one mandatory numeric accounting row."""

from __future__ import annotations

import asyncio
import logging

import pytest

from server.core import diagnostics, request_tail
from server.observability import ledger
from tests.unit._marked_transport_exit import Stopped, assert_row, prepare


@pytest.fixture(autouse=True)
def diagnostic_handlers():
    token = diagnostics._codes_only.set(False)
    handlers = [*logging.getLogger().handlers, logging.lastResort]
    saved = [(handler, handler.filters[:]) for handler in handlers if handler]
    try:
        yield
    finally:
        diagnostics._codes_only.reset(token)
        for handler, filters in saved:
            handler.filters[:] = filters


@pytest.mark.parametrize("transport", ["http", "mcp"])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("stage", ["handler", "post_rows"])
async def test_cancelled_marked_call_commits_once_without_losing_admitted_identity(
    monkeypatch, transport, private, stage
):
    world = prepare(monkeypatch, transport)
    world.private, world.stage = private, stage
    tail = request_tail._Tail()
    token = request_tail._tail.set(tail)
    task = asyncio.create_task(world.invoke())
    try:
        await asyncio.wait_for(world.ready.wait(), 1)
        task.cancel("private cancellation nonce")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert cancelled.value is world.primary
        assert tail.has_required, "marked cancellation lost required accounting"
        assert world.rows == []
        await request_tail.settle(tail)
        await request_tail.settle(tail)
        assert_row(world, transport, success=stage == "post_rows")
    finally:
        world.release.set()
        world.insert_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        request_tail._tail.reset(token)


@pytest.mark.parametrize("transport", ["http", "mcp"])
async def test_unmarked_cancellation_creates_no_marked_fallback(monkeypatch, transport):
    world = prepare(monkeypatch, transport)
    world.marked = False
    tail = request_tail._Tail()
    token = request_tail._tail.set(tail)
    task = asyncio.create_task(world.invoke())
    try:
        await asyncio.wait_for(world.ready.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await request_tail.settle(tail)
        assert not tail.has_required
        assert world.rows == world.queued == world.held == []
    finally:
        world.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        request_tail._tail.reset(token)


@pytest.mark.parametrize("transport", ["http", "mcp"])
async def test_primary_base_exception_identity_survives_capture(monkeypatch, transport):
    world = prepare(monkeypatch, transport)
    primary = world.control = Stopped("private control nonce")
    tail = request_tail._Tail()
    token = request_tail._tail.set(tail)
    try:
        with pytest.raises(Stopped) as stopped:
            await world.invoke()
        assert stopped.value is primary
        assert tail.has_required, "escaping control lost required accounting"
        await request_tail.settle(tail)
        assert_row(world, transport)
    finally:
        request_tail._tail.reset(token)


@pytest.mark.parametrize("transport", ["http", "mcp"])
async def test_capture_waits_for_handler_owned_child_final_charge(monkeypatch, transport):
    world = prepare(monkeypatch, transport)
    world.join_child = True
    tail = request_tail._Tail()
    token = request_tail._tail.set(tail)
    task = asyncio.create_task(world.invoke())
    try:
        await asyncio.wait_for(world.ready.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert world.child is not None and world.child.done()
        assert tail.has_required, "joined cleanup spend has no required accounting"
        await request_tail.settle(tail)
        assert_row(world, transport, joined=True)
    finally:
        world.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if world.child is not None:
            if not world.child.done():
                world.child.cancel()
            await asyncio.gather(world.child, return_exceptions=True)
        request_tail._tail.reset(token)


@pytest.mark.parametrize("transport", ["http", "mcp"])
@pytest.mark.parametrize("fault", ["construction", "registration"])
async def test_capture_failure_preserves_primary_control_and_latches_required_failure(
    monkeypatch, transport, fault
):
    world = prepare(monkeypatch, transport)
    primary = world.control = Stopped("private primary nonce")

    def fail(*args, **kwargs):
        raise RuntimeError("private accounting failure nonce")

    if fault == "construction":
        monkeypatch.setattr(ledger, "AgentEvent", fail)
    else:
        monkeypatch.setattr(request_tail, "defer_required", fail)
    tail = request_tail._Tail()
    token = request_tail._tail.set(tail)
    task = asyncio.create_task(world.invoke())
    try:
        with pytest.raises(Stopped) as stopped:
            await task
        assert stopped.value is primary
        assert tail.has_required and tail.required_failed
        with pytest.raises(request_tail.RequiredTailError) as failed:
            await request_tail.settle(tail)
        assert str(failed.value) == "Required request work was not confirmed"
        assert world.client.insert.await_count == 0
        assert world.rows == world.queued == world.held == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        request_tail._tail.reset(token)


@pytest.mark.parametrize("transport", ["http", "mcp"])
async def test_outer_tail_joins_one_commit_before_repeated_request_cancellation_escapes(
    monkeypatch, transport
):
    world = prepare(monkeypatch, transport)
    world.pause_insert = True

    async def app(scope, receive, send):
        await world.invoke()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"success"})

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        world.messages.append(message)

    task = asyncio.create_task(
        request_tail.RequestTailMiddleware(app)({"type": "http"}, receive, send)
    )
    try:
        await asyncio.wait_for(world.ready.wait(), 1)
        task.cancel("private cancellation nonce")
        await asyncio.wait_for(world.insert_started.wait(), 1)
        assert world.tail.has_required, "outer cleanup has no required cancellation row"
        assert not task.done() and world.rows == []
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and world.messages == []
        world.insert_release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert cancelled.value is world.primary
        assert world.messages == []
        assert_row(world, transport)
        await request_tail.settle(world.tail)
        assert world.client.insert.await_count == 1
    finally:
        world.release.set()
        world.insert_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
