"""What each bi-temporal setting changes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory.app import temporal_reads
from server.memory.domain.temporal_reads import Axes
from server.memory.store import temporal as store
from server.settings import settings

pytestmark = pytest.mark.temporal_journal


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _no(*_a: Any, **_k: Any) -> bool:
    return False


def _cutoff_store(monkeypatch, *, head: int, watermark: int) -> None:
    async def cut(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "head": head,
            "committed": head,
            "n": head,
            "seq": head,
            "known": head,
            "settled": head,  # every commit settled: the stable prefix is the head
            "first_at": datetime(2026, 1, 1, tzinfo=UTC),
        }

    async def wm(*_a: Any, **_k: Any) -> int:
        return watermark

    async def not_erased(*_a: Any, **_k: Any) -> Any:
        return store.NOT_ERASED

    monkeypatch.setattr(store, "cutoff", cut)
    monkeypatch.setattr(store, "rows_before_journal", _no)
    monkeypatch.setattr(store, "watermark", wm)
    monkeypatch.setattr(temporal_reads, "erased", not_erased)


def test_a_tail_past_memory_temporal_max_tail_is_not_ready(monkeypatch) -> None:
    _cutoff_store(monkeypatch, head=10, watermark=2)
    axes = Axes(valid=datetime.now(UTC), known=None)
    monkeypatch.setattr(settings, "memory_bitemporal_max_tail", 100)
    assert "projection_not_ready" not in _run(temporal_reads.cutoff("o", "n", axes)).gaps
    monkeypatch.setattr(settings, "memory_bitemporal_max_tail", 5)
    assert "projection_not_ready" in _run(temporal_reads.cutoff("o", "n", axes)).gaps


def test_a_token_ahead_of_the_head_waits_memory_temporal_read_wait_ms(monkeypatch) -> None:
    from server.memory.domain.temporal_reads import encode_token

    _cutoff_store(monkeypatch, head=3, watermark=3)
    monkeypatch.setattr(settings, "memory_bitemporal_read_wait_ms", 120)
    axes = Axes(valid=datetime.now(UTC), known=None)
    t0 = datetime.now(UTC)
    cut = _run(temporal_reads.cutoff("o", "n", axes, read_token=encode_token({"n": 9})))
    assert (datetime.now(UTC) - t0).total_seconds() >= 0.1
    assert "read_token_not_ready" in cut.gaps


def test_memory_temporal_lock_wait_s_bounds_the_publisher_lock() -> None:
    from server.memory.app import temporal_publisher

    assert temporal_publisher._locks._wait_s == settings.memory_bitemporal_lock_wait_s


def test_the_projection_worker_row() -> None:
    from server.workers.registry import _WORKERS

    (row,) = [w for w in _WORKERS if w.name == "memory_temporal_projection"]
    assert row.enabled == "memory_bitemporal_projection_worker_enabled"
    assert row.interval == "memory_bitemporal_projection_worker_interval_seconds"
