"""Scalar bookkeeping and consumers retain their own query attribution."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core import ch as ch_module
from server.core import ch_schema_guard
from server.core.schema import status
from server.core.vectors.admission import vector_work
from server.knowledge.store import source_candidates
from server.memory.app.search import ann_entity_boosts
from server.settings import settings
from tests.unit.test_vector_read_admission import Driver, Embedder, client


@pytest.mark.asyncio
async def test_nonvector_read_keeps_disabled_caller_shaped_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, driver = client(monkeypatch)
    monkeypatch.setattr(settings, "ch_caller_shaped_max_execution_s", 0.0)
    driver.release.set()
    assert await ch.query_all("SELECT 1") == []
    assert driver.started == 1


class Stream:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.sent = False

    async def __aenter__(self) -> Stream:
        self.opened = True
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.closed = True

    def __aiter__(self) -> Stream:
        return self

    async def __anext__(self) -> bytes:
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return b'{"namespace":"private","chunk_idx":0}\n'


class StreamingDriver(Driver):
    def __init__(self) -> None:
        super().__init__()
        self.stream = Stream()
        self.stream_settings: dict[str, Any] = {}

    async def raw_stream(self, _sql: str, **kwargs: Any) -> Stream:
        self.stream_settings = kwargs["settings"]
        return self.stream


@pytest.mark.asyncio
async def test_mirror_stream_clears_only_consumer_attribution_and_closes_in_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, _ = client(monkeypatch)
    driver = StreamingDriver()
    driver.release.set()
    monkeypatch.setattr(ch, "_client", driver)
    with vector_work("one-org", ["private"]):
        async with source_candidates.candidates(
            "SELECT namespace, cosineDistance(embedding, {q:Array(Float32)}) AS distance FROM loom.chunks",
            {"org": "one-org", "nss": ["private"], "q": [1.0, 0.0]},
            ch=ch,
        ) as rows:
            assert await anext(rows) == {"namespace": "private", "chunk_idx": 0}
            assert driver.stream.opened and not driver.stream.closed
            assert await ch.query_all("SELECT 1") == []
            assert "max_rows_to_read" not in driver.settings[-1]
    assert driver.stream.closed
    assert driver.stream_settings["max_rows_to_read"] == 1_000_000
    assert driver.stream_settings["read_overflow_mode"] == "throw"
    assert await ch.query_all("SELECT 1") == []
    assert "max_rows_to_read" not in driver.settings[-1]


@pytest.mark.asyncio
async def test_entity_boost_cold_schema_guard_keeps_ledger_scalar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, driver = client(monkeypatch)
    driver.release.set()
    monkeypatch.setattr(settings, "vector_scan_max_concurrent_per_org", 2)
    monkeypatch.setattr(ch_module, "guard_pending_schema", ch_schema_guard.guard_pending_schema)
    monkeypatch.setattr(
        ch_schema_guard, "_schema_guard", {"at": None, "tables": {}, "refreshing": False}
    )
    monkeypatch.setattr(ann_entity_boosts, "get_client", lambda: ch)

    async def cold_status() -> dict[str, Any]:
        await ch.query_all("SELECT 1")
        return {"pending": [], "applied": 1}

    monkeypatch.setattr(status, "schema_status", cold_status)
    result = await asyncio.wait_for(
        ann_entity_boosts._ann_entity_boosts(
            org="one-org",
            namespaces=["private"],
            agent="a",
            user_id="u",
            query_entities=["entity"],
            embedder=Embedder(),
        ),
        timeout=1,
    )
    assert result == {}
    assert driver.started == 2
    assert "max_rows_to_read" not in driver.settings[0]
    assert driver.settings[1]["max_rows_to_read"] == 1_000_000
    assert driver.settings[1]["read_overflow_mode"] == "throw"
    assert type(driver.settings[1]["max_rows_to_read"]) is int
    assert type(driver.settings[1]["max_memory_usage"]) is int
    assert driver.active == 0
