"""The usage-event flusher is process state a test must put back:
`_reset_for_tests` empties the buffer and stops the detached flush
task without touching any client, and the next recorder starts afresh."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.memory.store import usage as usage


class _CH:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserted.append((table, rows))


@pytest.mark.asyncio
async def test_reset_empties_the_buffer_and_stops_the_flusher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    await usage.record_usage_event(memory_id="m1", org="o", namespace="n", event_type="harm")
    assert len(usage._USAGE_BUFFER) == 1
    task = usage._USAGE_FLUSH_TASK
    assert task is not None and not task.done()

    usage._reset_for_tests()
    assert usage._USAGE_BUFFER == [] and usage._USAGE_FLUSH_TASK is None
    assert usage._USAGE_BUFFER_LOCK is None
    # Let the cancelled task unwind: its drain finds an empty buffer.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    await asyncio.sleep(0.7)
    assert ch.inserted == [], "a reset flusher must never write the dropped rows"


@pytest.mark.asyncio
async def test_next_recorder_starts_a_fresh_flusher(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    usage._reset_for_tests()
    await usage.record_usage_event(memory_id="m2", org="o", namespace="n", event_type="recall")
    assert usage._USAGE_FLUSH_TASK is not None and not usage._USAGE_FLUSH_TASK.done()
    await usage._flush_usage_buffer_now(background=True)
    assert [r["memory_id"] for _, rows in ch.inserted for r in rows] == ["m2"]
    usage._reset_for_tests()


@pytest.mark.asyncio
async def test_the_flush_loop_tags_its_inserts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop is started by whichever request records first, in a detached
    context; its inserts carry the flusher's own tag, not that request's and
    not the `untagged:` fallback."""
    from server.core.ch import current_query_tag, set_query_tag

    tags: list[str] = []

    class _TagCH(_CH):
        async def insert(self, table, rows, settings=None):
            tags.append(current_query_tag())

    monkeypatch.setattr("server.core.ch.get_client", lambda: _TagCH())
    monkeypatch.setattr(usage, "_USAGE_FLUSH_INTERVAL_S", 0.01)
    usage._reset_for_tests()
    set_query_tag("memory.search")
    await usage.record_usage_event(memory_id="m3", org="o", namespace="n", event_type="recall")
    await asyncio.sleep(0.1)
    usage._reset_for_tests()
    assert tags == [usage.USAGE_FLUSH_QUERY_TAG]
