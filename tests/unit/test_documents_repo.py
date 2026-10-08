"""Unit tests for server/knowledge/store/documents.py — mocked CH client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.knowledge.store.documents import (
    DocumentRow,
    get_many_by_uris,
    get_one,
    insert_many,
    insert_one,
)


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


def _doc_row() -> dict[str, Any]:
    return {
        "org": "clickhouse",
        "namespace": "ns",
        "source_uri": "kb://policy.md",
        "doc_id": "11111111-2222-3333-4444-555555555555",
        "title": "Policy",
        "content": "...",
        "tags": ["policy"],
        "metadata": '{"k":"v"}',
        "version": 1,
        "superseded_at": None,
        "ingested_at": datetime(2026, 5, 1, tzinfo=UTC),
    }


@pytest.mark.asyncio
async def test_get_one_filters_superseded_by_default() -> None:
    ch = StubCH()
    ch.query_one_returns.append(_doc_row())
    out = await get_one("clickhouse", "ns", "kb://policy.md", ch=ch)
    sql, params = ch.query_one_calls[0]
    assert "superseded_at IS NULL" in sql
    assert "FROM loom.documents FINAL" in sql
    assert params["uri"] == "kb://policy.md"
    assert isinstance(out, DocumentRow)
    assert out.source_uri == "kb://policy.md"


@pytest.mark.asyncio
async def test_get_one_includes_superseded_when_requested() -> None:
    """audit/replay paths fetch the latest row regardless of supersede state."""
    ch = StubCH()
    ch.query_one_returns.append(None)
    await get_one(
        "clickhouse",
        "ns",
        "kb://policy.md",
        include_superseded=True,
        ch=ch,
    )
    sql, _ = ch.query_one_calls[0]
    assert "superseded_at IS NULL" not in sql


@pytest.mark.asyncio
async def test_get_many_by_uris_emits_array_filter() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_doc_row(), _doc_row()])
    rows = await get_many_by_uris(
        "clickhouse",
        "ns",
        ["kb://a.md", "kb://b.md"],
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "source_uri IN {uris:Array(String)}" in sql
    assert params["uris"] == ["kb://a.md", "kb://b.md"]
    assert len(rows) == 2 and all(isinstance(r, DocumentRow) for r in rows)


@pytest.mark.asyncio
async def test_get_many_by_uris_empty_input_skips_ch() -> None:
    ch = StubCH()
    out = await get_many_by_uris("clickhouse", "ns", [], ch=ch)
    assert out == []
    assert ch.query_all_calls == []


@pytest.mark.asyncio
async def test_insert_one_forces_synchronous_visibility() -> None:
    ch = StubCH()
    await insert_one(_doc_row(), ch=ch)
    table, rows, settings = ch.insert_calls[0]
    assert table == "loom.documents"
    assert len(rows) == 1
    # search-visibility contract: next FINAL read sees this row
    assert settings.get("async_insert") == 0


@pytest.mark.asyncio
async def test_insert_many_returns_count_and_skips_on_empty() -> None:
    ch = StubCH()
    n = await insert_many([_doc_row(), _doc_row(), _doc_row()], ch=ch)
    assert n == 3
    n2 = await insert_many([], ch=ch)
    assert n2 == 0
    # Only one CH insert call (the empty one short-circuited)
    assert len(ch.insert_calls) == 1
