"""An insert learns its table's column types once, and a
process-lifetime task carries no request's context.

Pure: a fake driver client records what the wrapper hands it."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from clickhouse_connect.driver.external import ExternalData

import server.core.ch as ch_mod
from server.core.background import detached_task


class _FakeAsync:
    def __init__(self, *, fail_typed_inserts: int = 0) -> None:
        self.describes = 0
        self.inserts: list[dict[str, Any]] = []
        self.fail_typed_inserts = fail_typed_inserts

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        assert sql.startswith("DESCRIBE TABLE ")
        self.describes += 1

        class _R:
            column_names = ["name", "type", "default_type"]
            result_rows = [["a", "Int64", ""], ["b", "String", ""]]

        return _R()

    async def insert(
        self,
        table: str,
        data: Any,
        column_names: Any = None,
        column_type_names: Any = None,
        settings: Any = None,
    ) -> None:
        self.inserts.append(
            {
                "table": table,
                "columns": list(column_names),
                "types": column_type_names,
                "rows": len(data),
            }
        )
        if column_type_names is not None and self.fail_typed_inserts > 0:
            self.fail_typed_inserts -= 1
            raise RuntimeError("Type mismatch: schema changed")


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ch_mod.describe_cache_clear()
    ch_mod.set_query_tag("")
    yield
    ch_mod.describe_cache_clear()
    ch_mod.set_query_tag("")


def _client(fake: _FakeAsync, monkeypatch: pytest.MonkeyPatch) -> ch_mod.CHClient:
    c = ch_mod.CHClient()
    monkeypatch.setattr(type(c), "client", property(lambda self: fake))
    return c


@pytest.mark.asyncio
async def test_types_are_described_once_per_table_and_handed_to_the_driver(monkeypatch) -> None:
    fake = _FakeAsync()
    c = _client(fake, monkeypatch)
    await c.insert("loom.t", [{"a": 1, "b": "x"}])
    await c.insert("loom.t", [{"b": "y", "a": 2}, {"b": "z", "a": 3}])
    await c.insert("loom.t", [{"a": 4, "b": "w"}])
    assert fake.describes == 1
    assert [i["types"] for i in fake.inserts] == [
        ["Int64", "String"],
        ["String", "Int64"],
        ["Int64", "String"],
    ]
    assert [i["columns"] for i in fake.inserts][1] == ["b", "a"]


@pytest.mark.asyncio
async def test_unknown_column_falls_back_to_the_driver_lookup(monkeypatch) -> None:
    fake = _FakeAsync()
    c = _client(fake, monkeypatch)
    await c.insert("loom.t", [{"a": 1, "zzz": 0}])
    assert fake.inserts[-1]["types"] is None  # driver describes for itself


@pytest.mark.asyncio
async def test_a_failed_typed_insert_drops_the_cache_and_retries_untyped(monkeypatch) -> None:
    async def _no_retry(self: Any, op_name: str, fn: Any, **_kw: Any) -> Any:
        return await fn()

    monkeypatch.setattr(ch_mod.CHClient, "_with_retry", _no_retry)
    fake = _FakeAsync(fail_typed_inserts=1)
    c = _client(fake, monkeypatch)
    await c.insert("loom.t", [{"a": 1, "b": "x"}])
    assert [i["types"] for i in fake.inserts] == [["Int64", "String"], None]
    assert "loom.t" not in ch_mod._COLUMN_TYPES  # forgotten; re-described next time
    await c.insert("loom.t", [{"a": 1, "b": "x"}])
    assert fake.describes == 2 and fake.inserts[-1]["types"] == ["Int64", "String"]


@pytest.mark.asyncio
async def test_detached_task_does_not_inherit_the_callers_query_tag() -> None:
    seen: dict[str, str] = {}

    async def probe() -> None:
        seen["tag"] = ch_mod.current_query_tag()

    ch_mod.set_query_tag("memory.ask req-leaky")
    inherited = asyncio.create_task(probe())
    await inherited
    assert seen["tag"] == "memory.ask req-leaky"  # plain create_task copies the context
    detached = detached_task(probe(), name="probe")
    await detached
    assert seen["tag"] == ""  # a process task carries no request
    assert detached.get_name() == "probe"


@pytest.mark.asyncio
async def test_external_batch_keeps_write_settings_and_deduplication(monkeypatch) -> None:
    fake = AsyncMock()
    fake.command.return_value = None
    client = _client(fake, monkeypatch)
    batch = ExternalData(
        file_name="updates", data=b'{"id":1}', fmt="JSONEachRow", structure="id UInt64"
    )
    await client.execute(
        "INSERT INTO loom.t SELECT id FROM updates",
        settings={"async_insert": 0},
        external_data=batch,
    )
    call = fake.command.await_args
    assert call.kwargs["external_data"] is batch
    assert call.kwargs["parameters"] == {}
    assert call.kwargs["settings"]["async_insert"] == 0
    assert call.kwargs["settings"]["insert_deduplication_token"]
