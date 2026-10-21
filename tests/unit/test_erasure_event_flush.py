"""An erasure waits for buffered event inserts before counting its rows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.erasure.store import steps_ledgers as steps
from server.observability import events_stream as stream


@pytest.fixture
def event_store(monkeypatch):
    state = SimpleNamespace(
        rows=[],
        started=asyncio.Event(),
        release=asyncio.Event(),
        draining=asyncio.Event(),
        deletes=0,
    )
    monkeypatch.setattr(stream, "_queue", [])
    monkeypatch.setattr(stream, "_ensure_drain", lambda: None)
    original = stream.flush_now

    async def drain():
        state.draining.set()
        return await original()

    monkeypatch.setattr(stream, "flush_now", drain)

    class _CH:
        async def insert(self, table, rows, **kwargs):
            assert table == "loom.events"
            state.started.set()
            await state.release.wait()
            state.rows.extend(rows)

    async def delete(table, *, org, scope_where, params, ch):
        state.deletes += 1
        assert table == "loom.events"

        def matches(row):
            if row["org"] != org:
                return False
            if "uid" in params:
                return row["user_id"] == params["uid"]
            namespaces = params["ns"] if isinstance(params["ns"], list) else [params["ns"]]
            return row["namespace"] in namespaces

        before = len(state.rows)
        state.rows[:] = [row for row in state.rows if not matches(row)]
        return before - len(state.rows)

    monkeypatch.setattr("server.core.ch.get_client", _CH)
    monkeypatch.setattr(steps, "delete_scoped", delete)
    return state


def _record():
    stream.record(stream.BUDGET_VIOLATION_KIND, {"org": "o", "namespace": "n", "user_id": "u"})


async def _erase(scope):
    if scope == "user":
        return await steps.delete_events_by_user("o", "u")
    return await steps.delete_events_by_namespace("o", ["n", "other"] if scope == "list" else "n")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["namespace", "list", "user"])
@pytest.mark.parametrize("buffer", ["queued", "inflight", "empty"])
async def test_erasure_waits_for_events_to_land(event_store, scope, buffer):
    state = event_store
    flushing = None
    if buffer != "empty":
        _record()
    if buffer == "inflight":
        flushing = asyncio.create_task(stream.flush_now())
        await state.started.wait()
    state.draining.clear()
    erasing = asyncio.create_task(_erase(scope))
    try:
        await state.draining.wait()
        await asyncio.sleep(0)
        if buffer == "inflight":
            assert not erasing.done()
            assert state.deletes == 0
        state.release.set()
        assert await erasing == int(buffer != "empty")
        if flushing is not None:
            await flushing
        await stream.flush_now()
        assert state.rows == []
        assert stream.queued() == 0
    finally:
        state.release.set()
        await asyncio.gather(erasing, *([flushing] if flushing else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_erasure_keeps_its_uncommitted_events(event_store):
    _record()
    erasing = asyncio.create_task(_erase("namespace"))
    try:
        await asyncio.wait_for(event_store.started.wait(), timeout=1)
        erasing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await erasing
        assert event_store.deletes == 0
        assert stream.queued() == 1
        event_store.release.set()
        assert await _erase("namespace") == 1
        assert event_store.rows == []
    finally:
        erasing.cancel()
        await asyncio.gather(erasing, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_failed_telemetry_insert_leaves_nothing_to_reappear(event_store, monkeypatch):
    async def failed(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr("server.core.ch.get_client", lambda: SimpleNamespace(insert=failed))
    _record()
    assert await _erase("namespace") == 0
    assert stream.queued() == 0
    assert await stream.flush_now() == 0
    assert event_store.rows == []


@pytest.mark.asyncio
async def test_erasure_times_out_without_losing_its_pending_event(event_store, monkeypatch):
    monkeypatch.setattr(steps, "_EVENT_DRAIN_S", 0.01)
    _record()
    with pytest.raises(TimeoutError):
        await _erase("namespace")
    assert event_store.deletes == 0
    assert stream.queued() == 1
    event_store.release.set()
    assert await _erase("namespace") == 1
    assert event_store.rows == []


@pytest.mark.asyncio
async def test_cancelling_erasure_does_not_cancel_a_flush_it_waits_for(event_store):
    _record()
    flushing = asyncio.create_task(stream.flush_now())
    await event_store.started.wait()
    event_store.draining.clear()
    erasing = asyncio.create_task(_erase("user"))
    try:
        await event_store.draining.wait()
        await asyncio.sleep(0)
        erasing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await erasing
        assert event_store.deletes == 0
        assert not flushing.done()
        event_store.release.set()
        await flushing
        assert await _erase("user") == 1
        assert event_store.rows == []
    finally:
        event_store.release.set()
        await asyncio.gather(flushing, erasing, return_exceptions=True)


def test_flushes_can_contend_on_successive_event_loops(event_store):
    async def contend():
        event_store.started, event_store.release = asyncio.Event(), asyncio.Event()
        _record()
        first = asyncio.create_task(stream.flush_now())
        await event_store.started.wait()
        second = asyncio.create_task(stream.flush_now())
        await asyncio.sleep(0)
        event_store.release.set()
        assert await first == 1
        assert await second == 0

    asyncio.run(contend())
    asyncio.run(contend())
    assert len(event_store.rows) == 2
