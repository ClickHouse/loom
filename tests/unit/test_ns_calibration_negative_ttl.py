"""A per-namespace calibration miss (no row yet, or a failed read) is cached
for a minute, not for the life of the process; a found row is cached."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import embedder_calibration as ec
from server.settings import settings


class _CH:
    def __init__(self) -> None:
        self.reads = 0
        self.fail = True

    async def query_one(self, sql: str, params: Any = None) -> Any:
        self.reads += 1
        if self.fail:
            raise RuntimeError("Code: 210. Connection reset")
        return None


@pytest.mark.asyncio
async def test_a_failed_read_is_retried_after_the_negative_ttl(monkeypatch) -> None:
    ch, clock = _CH(), {"t": 1000.0}
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(ec.time, "monotonic", lambda: clock["t"])
    ec.clear_ns_overrides()
    assert await ec.get_namespace_calibration("o", "ns") is None
    assert await ec.get_namespace_calibration("o", "ns") is None
    assert ch.reads == 1  # cached, CH is not hammered
    clock["t"] += settings.memory_calibration_namespace_miss_ttl_s + 1
    ch.fail = False
    assert await ec.get_namespace_calibration("o", "ns") is None
    assert ch.reads == 2  # read again once the miss aged out
    ec.clear_ns_overrides()
