"""Captured marked requests use awaited insertion outside the ordinary queue."""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.core import ch_writes, diagnostics
from server.core.diagnostics import private_work
from server.observability import events


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


@pytest.fixture
def writer(monkeypatch):
    rows = []

    async def insert(table, data, *, settings, required_columns):
        assert required_columns == frozenset({"mirror_marked"})
        rows.append((table, copy.deepcopy(data), dict(settings)))

    client = SimpleNamespace(insert=AsyncMock(side_effect=insert))
    emitter = events.EventEmitter()
    monkeypatch.setattr(events, "get_client", lambda: client)
    monkeypatch.setattr(events, "get_emitter", lambda: emitter)
    monkeypatch.setattr(events, "stamp_row", lambda row: row)
    monkeypatch.setattr(events, "for_this_cell", lambda event: event)
    return SimpleNamespace(client=client, emitter=emitter, rows=rows)


def captured_event(**changes):
    values = dict(
        ts=datetime.fromtimestamp(0, UTC),
        org="org",
        namespace="team",
        agent="agent",
        user_id="user",
        session_id="session",
        operation="memory.search",
        request_id="request",
        latency_ms=13,
        success=False,
        input_tokens=17,
        output_tokens=19,
        embedding_tokens=23,
        upstream_cost_usd=0.125,
        bytes_written=29,
        error_code="UNAVAILABLE",
        kind="request",
        args={"field": "digest"},
        model="model",
        llm_calls=2,
        subject="subject",
        transport="http",
        mirror_marked=True,
    )
    values.update(changes)
    return events.AgentEvent(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("batching", [False, True])
async def test_waited_writer_preserves_row_and_omits_process_clock(monkeypatch, writer, batching):
    monkeypatch.setattr(
        ch_writes,
        "settings",
        SimpleNamespace(
            ch_batched_inserts=batching,
            ch_batched_insert_wait_min_ms=3,
            ch_batched_insert_wait_max_ms=11,
            ch_batched_insert_background_wait_max_ms=17,
        ),
    )
    event = captured_event()
    expected = event.to_row()
    del expected["ts"]
    expected["args"] = {}
    expected["memory_key"] = ""
    original = copy.deepcopy(event)
    before = writer.emitter.stats()
    with private_work():
        await events.write_marked_request_event(event)
    assert event == original
    assert len(writer.rows) == 1
    table, rows, settings = writer.rows[0]
    assert table == "loom.agent_events" and rows == [expected]
    assert "ts" not in rows[0] and rows[0]["mirror_marked"] == 1
    assert settings["wait_for_async_insert"] == 1
    assert settings["async_insert"] == int(batching)
    if batching:
        assert settings["async_insert_use_adaptive_busy_timeout"] == 1
        assert settings["async_insert_busy_timeout_min_ms"] == 3
        assert settings["async_insert_busy_timeout_max_ms"] == 11
    else:
        assert settings == {"async_insert": 0, "wait_for_async_insert": 1}
    assert writer.emitter.stats() == before
    assert writer.emitter.queue.empty()


@pytest.mark.asyncio
async def test_writer_drops_untrusted_projections_even_without_the_transport(writer):
    event = captured_event(
        memory_key="private-key-canary",
        args={
            "out_ch_statements": "3",
            "out_ch_read_rows": "private-row-canary",
            "out_ch_read_bytes": "-1",
            "out_injected_facts": "1.5",
            "out_injected_chunks": "２",
            "out_ch_elapsed_ms": "inf",
            "out_tags": "private-tag-canary",
            "error": "private-error-canary",
        },
    )
    with private_work():
        await events.write_marked_request_event(event)
    row = writer.rows[0][1][0]
    assert row["args"] == {"out_ch_statements": "3"}
    assert row["memory_key"] == ""
    assert "private-" not in str(row)
    assert event.memory_key == "private-key-canary"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,marker,private",
    [
        ("worker", True, True),
        ("request", False, True),
        ("request", 1, True),
        ("request", True, False),
    ],
)
async def test_invalid_capture_refuses_before_client_access(monkeypatch, kind, marker, private):
    access = Mock(side_effect=AssertionError("invalid capture must not touch the client"))
    monkeypatch.setattr(events, "get_client", access)
    event = captured_event(kind=kind, mirror_marked=marker)
    if private:
        with private_work(), pytest.raises(ValueError, match="server classification"):
            await events.write_marked_request_event(event)
    else:
        with pytest.raises(ValueError, match="server classification"):
            await events.write_marked_request_event(event)
    access.assert_not_called()


@pytest.mark.asyncio
async def test_insert_failure_is_owned_and_never_queued(writer):
    original = RuntimeError("private backend canary")
    writer.client.insert.side_effect = original
    before = writer.emitter.stats()
    with private_work(), pytest.raises(RuntimeError) as caught:
        await events.write_marked_request_event(captured_event())
    assert caught.value is original
    assert writer.emitter.stats() == before
    assert writer.emitter.queue.empty()


@pytest.mark.asyncio
async def test_cancelled_insert_leaves_no_background_writer(writer):
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended(*args, **kwargs):
        entered.set()
        await release.wait()

    writer.client.insert.side_effect = suspended
    before = writer.emitter.stats()

    async def deliver():
        with private_work():
            await events.write_marked_request_event(captured_event())

    task = asyncio.create_task(deliver())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done() and task.cancelled()
        assert writer.emitter.stats() == before
        assert writer.emitter.queue.empty()
        writer.client.insert.assert_awaited_once()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_writer_still_refuses_an_ordinary_namespace(writer):
    with private_work(), pytest.raises(ValueError, match="require a mirror"):
        await events.write_private_event(captured_event(kind="worker"))
    writer.client.insert.assert_not_awaited()
