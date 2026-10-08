"""The two event flush loops run in a detached context, so each names its
own inserts in the query log instead of the `untagged:` frame fallback."""

from __future__ import annotations

import asyncio

from server.core import ch
from server.observability import events, events_stream


def test_agent_events_drain_loop_tags_its_inserts(monkeypatch) -> None:
    seen: list[str] = []
    emitter = events.EventEmitter()
    emitter._running = True

    async def _flush_once() -> None:
        seen.append(ch.current_query_tag())
        emitter._running = False

    monkeypatch.setattr(events, "FLUSH_INTERVAL_S", 0)
    monkeypatch.setattr(emitter, "_flush_once", _flush_once)
    asyncio.run(emitter._drain_loop())
    assert seen == [events.FLUSH_QUERY_TAG]


def test_events_stream_drain_loop_tags_its_inserts(monkeypatch) -> None:
    seen: list[str] = []

    async def _flush_now() -> int:
        seen.append(ch.current_query_tag())
        monkeypatch.setattr(events_stream, "_stopping", True)
        return 0

    monkeypatch.setattr(events_stream, "_stopping", False)
    monkeypatch.setattr(events_stream, "_interval_s", lambda: 0)
    monkeypatch.setattr(events_stream, "flush_now", _flush_now)
    asyncio.run(events_stream._drain_loop())
    assert seen == [events_stream.FLUSH_QUERY_TAG]
