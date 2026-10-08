"""The ClickHouse client asks for the filesystem cache on every session — the
setting behind a 20× search-latency swing on Cloud services whose profile
ships it off — and the knob to turn that off exists."""

from __future__ import annotations

from typing import Any

import pytest

import server.core.ch as ch_mod
from server.core import ch_writes
from server.settings import settings


@pytest.mark.asyncio
async def test_session_settings_enable_filesystem_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_get_async_client(**kw: Any) -> Any:
        captured.update(kw)

        class _C:
            async def close(self) -> None: ...

        return _C()

    monkeypatch.setattr(ch_mod.clickhouse_connect, "get_async_client", _fake_get_async_client)
    c = ch_mod.CHClient()
    await c.connect()
    assert captured["settings"]["enable_filesystem_cache"] == 1
    # Committed writes are synchronous (the async
    # buffer does not batch at loom's write rates); block dedup stays off.
    assert captured["settings"]["async_insert"] == 0
    assert captured["settings"]["wait_for_async_insert"] == 1
    assert captured["settings"]["insert_deduplicate"] == 0


@pytest.mark.asyncio
async def test_keepalive_timeout_reaches_the_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool closes idle connections before the Cloud load
    balancer does; the knob is passed to the async client."""
    captured: dict[str, Any] = {}

    async def _fake_get_async_client(**kw: Any) -> Any:
        captured.update(kw)

        class _C:
            async def close(self) -> None: ...

        return _C()

    monkeypatch.setattr(ch_mod.clickhouse_connect, "get_async_client", _fake_get_async_client)
    monkeypatch.setattr(settings, "ch_keepalive_timeout", 7.5)
    await ch_mod.CHClient().connect()
    assert captured["keepalive_timeout"] == 7.5
    assert captured["connector_limit"] == settings.ch_pool_maxsize


def test_committed_idiom_passes_through_and_fire_and_forget_turns_dedup_off() -> None:
    """A committed insert is sent as the call site wrote it; fire-and-forget
    gains `async_insert_deduplicate = 0` unless the call site set it."""
    committed = {"async_insert": 0, "wait_for_async_insert": 1}
    assert ch_writes.normalize_insert_settings(committed) == committed
    ff = ch_writes.normalize_insert_settings({"async_insert": 1, "wait_for_async_insert": 0})
    assert ff == {"async_insert": 1, "wait_for_async_insert": 0, "async_insert_deduplicate": 0}
    pinned = {"async_insert": 1, "wait_for_async_insert": 0, "async_insert_deduplicate": 1}
    assert ch_writes.normalize_insert_settings(pinned) == pinned
    assert ch_writes.normalize_insert_settings(None) is None
