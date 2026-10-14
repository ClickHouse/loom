"""An insert into a coded table rebuilt at another width since the last
reading: ClickHouse refuses it (code 190), the insert reads the widths afresh,
refuses by name a vector the caller wrote, and pads its own placeholders again.

Pure: the driver checks vector lengths against the table's width as a coded
column does; the probe reads the same width from system.columns."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.core.ch as ch_mod
from server.core.errors import CodedWidthMismatchError
from server.core.vectors import codes_state

COLUMNS = ("embedding", "memory_key_embedding")


class _Table:
    """One coded `loom.memory_embeddings`, as the driver and the probe see it."""

    def __init__(self, width: int) -> None:
        self.width = width
        self.attempts = 0
        self.probes = 0
        self.error = ""  # every insert fails with this when set

    # The probe's client.
    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "system.columns" in sql:
            self.probes += 1
            codec = f"CODEC(Quantized('rabitq', {self.width}, 0))"
            return [
                {"table": "memory_embeddings", "name": c, "compression_codec": codec}
                for c in COLUMNS
            ]
        return []

    # The insert's driver.
    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        class _Described:
            column_names = ["name", "type", "default_type"]
            result_rows = [[c, "Array(BFloat16)", ""] for c in COLUMNS]

        return _Described()

    async def insert(self, table: str, data: Any, **_: Any) -> None:
        self.attempts += 1
        if self.error:
            raise RuntimeError(self.error)
        if any(len(vector) != self.width for row in data for vector in row):
            raise RuntimeError("Code: 190. DB::Exception: Array sizes do not match")


@pytest.fixture
async def table(monkeypatch: pytest.MonkeyPatch) -> _Table:
    async def _no_retry(self: Any, op_name: str, fn: Any, **_kw: Any) -> Any:
        return await fn()

    monkeypatch.setattr(ch_mod.CHClient, "_with_retry", _no_retry)
    ch_mod.describe_cache_clear()
    table = _Table(3072)
    await codes_state.probe(table)
    table.width = 1024  # rebuilt outside Loom, after the reading
    return table


def _client(table: _Table, monkeypatch: pytest.MonkeyPatch) -> ch_mod.CHClient:
    c = ch_mod.CHClient()
    monkeypatch.setattr(type(c), "client", property(lambda self: table))
    return c


@pytest.mark.asyncio
async def test_a_write_with_no_vector_lands_at_the_new_width(table, monkeypatch) -> None:
    rows = [{"embedding": [], "memory_key_embedding": []}]
    await _client(table, monkeypatch).insert("loom.memory_embeddings", rows)
    assert table.attempts == 2
    assert [len(rows[0][c]) for c in COLUMNS] == [1024, 1024]


@pytest.mark.asyncio
async def test_a_vector_at_the_old_width_is_refused_by_name_once(table, monkeypatch) -> None:
    rows = [{"embedding": [0.1] * 3072, "memory_key_embedding": []}]
    with pytest.raises(CodedWidthMismatchError, match="stores 1024-dim vectors"):
        await _client(table, monkeypatch).insert("loom.memory_embeddings", rows)
    assert table.attempts == 1


@pytest.mark.asyncio
async def test_another_error_passes_through_without_a_probe(table, monkeypatch) -> None:
    table.error = "Code: 241. DB::Exception: Memory limit exceeded"
    probes = table.probes
    with pytest.raises(RuntimeError, match="Code: 241"):
        await _client(table, monkeypatch).insert("loom.memory_embeddings", [_full(3072)])
    assert table.probes == probes


@pytest.mark.asyncio
async def test_a_190_the_fresh_reading_does_not_explain_is_raised_once(table, monkeypatch) -> None:
    table.width = 3072  # not rebuilt after all
    table.error = "Code: 190. DB::Exception: Array sizes do not match"
    with pytest.raises(RuntimeError, match="Code: 190"):
        await _client(table, monkeypatch).insert("loom.memory_embeddings", [_full(3072)])
    assert table.attempts == 1


@pytest.mark.asyncio
async def test_a_retry_that_fails_again_is_raised(table, monkeypatch) -> None:
    rows = [{"embedding": [], "memory_key_embedding": []}]
    client = _client(table, monkeypatch)
    real_insert = table.insert

    async def rebuilt_again(t: str, data: Any, **kw: Any) -> None:
        if table.attempts == 1:
            table.width = 8  # rebuilt once more before the retry
        await real_insert(t, data, **kw)

    table.insert = rebuilt_again  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Code: 190"):
        await client.insert("loom.memory_embeddings", rows)
    assert table.attempts == 2


def _full(width: int) -> dict[str, list[float]]:
    return {c: [0.1] * width for c in COLUMNS}


@pytest.mark.asyncio
async def test_an_older_probe_answering_after_the_refusal_keeps_the_new_width(
    table, monkeypatch
) -> None:
    """A probe that read the old width before the rebuild answers only after the
    one the 190 started has published: the insert is refused by name, and the
    new width stays."""
    table.width = 3072  # the background probe below reads the table before the rebuild
    old_answer = asyncio.Event()
    read = table.query_all

    async def held(sql: str, params: Any = None) -> list[dict[str, Any]]:
        rows = await read(sql, params)
        await old_answer.wait()
        return rows

    table.query_all = held  # type: ignore[method-assign]
    older = codes_state._start_refresh(since=0.0)  # as a stale reading starts it
    await asyncio.sleep(0)
    table.query_all = read  # type: ignore[method-assign]
    table.width = 1024  # rebuilt outside Loom
    insert = _client(table, monkeypatch).insert("loom.memory_embeddings", [_full(3072)])
    with pytest.raises(CodedWidthMismatchError, match="stores 1024-dim vectors"):
        await asyncio.wait_for(insert, 2)  # never wait on the older probe
    old_answer.set()
    await older
    assert codes_state.stored_width("memory_embeddings", "embedding") == 1024
