"""Person-query evidence excludes reads from independent background tasks."""

from __future__ import annotations

import asyncio
from contextvars import Context
from typing import Any

import pytest

from server.auth import scim_people
from server.core import ch, rmt_carry
from server.settings import settings
from tests.integration.test_scim_person_live_ch import people as person_fixture
from tests.unit.test_ch_client_hygiene import _client, _FakeAsync


class Queries:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.background_done = asyncio.Event()
        self.block_person = False
        self.transport: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def _read(self, sql: str) -> dict[str, Any]:
        self.transport.append((sql, ch.statement_settings(sql, cls="read")))
        if sql == "SELECT 1 AS person" and self.block_person:
            self.started.set()
            await self.background_done.wait()
        return {"n": 0, "at": None, "altered": None}

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
        return await self._read(sql)

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return [await self._read(sql)]


@pytest.fixture
async def recording(monkeypatch):
    client = Queries()
    monkeypatch.setattr(ch, "get_client", lambda: client)
    stream = person_fixture.__wrapped__(monkeypatch)
    observed = await anext(stream)
    try:
        yield observed, client
    finally:
        await stream.aclose()


@pytest.mark.parametrize("read_timeout", [0, 2.5])
@pytest.mark.parametrize("many", [False, True])
async def test_background_schema_reads_do_not_enter_person_evidence(
    recording, monkeypatch, read_timeout, many
):
    observed, client = recording
    monkeypatch.setattr(settings, "ch_timeout_read_s", read_timeout)
    client.block_person = True

    async def background():
        await client.started.wait()
        try:
            with ch.query_tag("rmt_carry.columns"):
                await rmt_carry._schema_stamp(client)
        finally:
            client.background_done.set()

    watcher = asyncio.create_task(background(), context=Context())
    try:
        with ch.statement_timeout(1.75), ch.query_tag("auth.scim_people"):
            query = client.query_all if many else client.query_one
            await query("SELECT 1 AS person")
        await watcher
    finally:
        if not watcher.done():
            watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    assert len(client.transport) == 2
    assert any("system.tables" in sql for sql, _ in client.transport)
    assert [sql for sql, _, _ in observed.results] == ["SELECT 1 AS person"]
    assert 0 < observed.results[0][1]["max_execution_time"] <= 1.75


@pytest.mark.parametrize("many", [False, True])
async def test_unbounded_person_reads_remain_visible(recording, monkeypatch, many):
    observed, client = recording
    monkeypatch.setattr(settings, "ch_timeout_read_s", 0)
    with ch.query_tag("auth.scim_people"):
        query = client.query_all if many else client.query_one
        await query("SELECT 1 AS person")
    assert len(observed.results) == 1
    with pytest.raises(KeyError, match="max_execution_time"):
        assert 0 < observed.results[0][1]["max_execution_time"] <= 1.75


@pytest.mark.parametrize("read_timeout", [0, 2.5])
@pytest.mark.parametrize("many", [False, True])
async def test_actual_person_reader_applies_its_finite_budget_to_driver(
    monkeypatch, read_timeout, many
):
    driver = _FakeAsync()
    client = _client(monkeypatch, driver, ch_timeout_read_s=read_timeout)
    monkeypatch.setattr(ch, "get_client", lambda: client)
    decision = scim_people._Decision("org", "owner", "owner@example.test", scim_people._now() + 10)
    with ch.statement_timeout(1.75), ch.query_tag("auth.scim_people"):
        await decision.query("SELECT 1 AS person", {}, many=many)
    calls = [applied for kind, sql, applied in driver.calls if sql == "SELECT 1 AS person"]
    assert len(calls) == 1
    assert 0 < calls[0]["max_execution_time"] <= 1.75
