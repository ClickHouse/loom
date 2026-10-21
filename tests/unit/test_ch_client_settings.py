"""The ClickHouse client asks for the filesystem cache on every session — the
setting behind a 20× search-latency swing on Cloud services whose profile
ships it off — and the knob to turn that off exists."""

from __future__ import annotations

from types import SimpleNamespace
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
    # Every session reads and writes UTC, whatever the server's <timezone>.
    assert captured["settings"]["session_timezone"] == "UTC"
    # ...and reads a bare DateTime in the client's zone (UTC), not the host's.
    assert captured["tz_source"] == "server"


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
    # Streaming reads have connections of their own beside the pool.
    assert captured["connector_limit"] == settings.ch_pool_maxsize + ch_mod.STREAM_SLOTS


@pytest.mark.asyncio
async def test_an_unbounded_pool_leaves_the_connector_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_get_async_client(**kw: Any) -> Any:
        captured.update(kw)
        return SimpleNamespace()

    monkeypatch.setattr(ch_mod.clickhouse_connect, "get_async_client", _fake_get_async_client)
    monkeypatch.setattr(settings, "ch_pool_maxsize", 0)
    await ch_mod.CHClient().connect()
    assert captured["connector_limit"] == captured["connector_limit_per_host"] == 0


def test_async_inserts_are_deduplicated_by_token_only() -> None:
    """Fire-and-forget and batched inserts both turn the async dedup switch on
    with the batch's token, so the check is by token, never by content."""
    ff = ch_writes.dedup_settings("t", {"async_insert": 1, "wait_for_async_insert": 0})
    assert ff == {
        "async_insert": 1,
        "wait_for_async_insert": 0,
        "insert_deduplication_token": "t",
        "async_insert_deduplicate": 1,
    }
    batched = ch_writes.dedup_settings("t", {"async_insert": 1, "wait_for_async_insert": 1})
    assert batched["async_insert_deduplicate"] == 1
    assert batched["insert_deduplication_token"] == "t"
    assert "insert_deduplicate" not in batched


def test_batched_inserts_wait_for_their_part_and_read_the_wait_bounds_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batched insert is answered once its part is written, and its wait bounds
    are read at call time (live settings); a minimum above the maximum is capped."""
    monkeypatch.setattr(settings, "ch_batched_insert_wait_min_ms", 10)
    monkeypatch.setattr(settings, "ch_batched_insert_wait_max_ms", 50)
    assert ch_writes.batched_insert_settings() == {
        "async_insert": 1,
        "wait_for_async_insert": 1,
        "async_insert_use_adaptive_busy_timeout": 1,
        "async_insert_busy_timeout_min_ms": 10,
        "async_insert_busy_timeout_max_ms": 50,
    }
    monkeypatch.setattr(settings, "ch_batched_insert_wait_min_ms", 80)
    s = ch_writes.batched_insert_settings()
    assert s["async_insert_busy_timeout_min_ms"] == 50 == s["async_insert_busy_timeout_max_ms"]
    # A retried batch still lands once: the token goes through the async switch.
    token = ch_writes.dedup_settings("t", s)
    assert token["async_insert_deduplicate"] == 1 and token["insert_deduplication_token"] == "t"


def test_background_inserts_take_the_longer_wait_and_the_switch_sends_them_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An insert no request waits on waits up to the background bound; with
    `ch_batched_inserts` off a batched insert is sent committed, or as a waited async
    insert on the profile's timeouts where it was one before the switch, read per call."""
    monkeypatch.setattr(settings, "ch_batched_insert_wait_min_ms", 10)
    monkeypatch.setattr(settings, "ch_batched_insert_background_wait_max_ms", 200)
    s = ch_writes.batched_insert_settings(background=True)
    assert s["async_insert"] == 1 and s["async_insert_busy_timeout_max_ms"] == 200
    assert s["async_insert_busy_timeout_min_ms"] == 10
    monkeypatch.setattr(settings, "ch_batched_inserts", False)
    committed = {"async_insert": 0, "wait_for_async_insert": 1}
    assert ch_writes.batched_insert_settings() == committed
    assert ch_writes.batched_insert_settings(background=True) == committed
    profile_async = {"async_insert": 1, "wait_for_async_insert": 1}
    assert ch_writes.batched_insert_settings(async_when_off=True) == profile_async
    assert ch_writes.batched_insert_settings(background=True, async_when_off=True) == profile_async
