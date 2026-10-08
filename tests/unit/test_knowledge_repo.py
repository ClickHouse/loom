"""Unit tests for server/knowledge/store/chunks.py — mocked CH client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
from server.knowledge.store.chunks import (
    ChunkRow,
    count_active_by_uri,
    count_active_for_doc,
    insert_chunks,
    list_for_doc,
    mark_doc_superseded,
    mark_docs_superseded,
)


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


def _chunk_row() -> dict[str, Any]:
    return {
        "org": "clickhouse",
        "namespace": "ns",
        "source_uri": "kb://policy.md",
        "chunk_idx": 0,
        "content": "...",
        "tags": ["policy"],
        "doc_metadata": "{}",
        "chunk_metadata": "{}",
        "ingested_at": datetime(2026, 5, 1, tzinfo=UTC),
        "doc_superseded_at": None,
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1536,
        "embedding": [0.1] * 1536,
        "created_at": datetime(2026, 5, 1, tzinfo=UTC),
        "embed_attempts": 0,
        "embed_last_error": "",
        "embed_failed_at": None,
    }


@pytest.mark.asyncio
async def test_list_for_doc_filters_and_hydrates() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_chunk_row(), _chunk_row()])
    rows = await list_for_doc(
        "clickhouse",
        "ns",
        "kb://policy.md",
        embedding_model="text-embedding-3-small",
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "doc_superseded_at IS NULL" in sql
    assert "ORDER BY chunk_idx ASC" in sql
    assert params["em"] == "text-embedding-3-small"
    assert len(rows) == 2 and all(isinstance(r, ChunkRow) for r in rows)


@pytest.mark.asyncio
async def test_mark_doc_superseded_lists_the_ingestions_instead_of_updating() -> None:
    """A supersede inserts the live (model, uri, doc_version)
    tuples into loom.chunk_supersessions; no UPDATE of loom.chunks, which
    left a patch part that turns the codes-first vector read off."""
    ch = StubCH()
    await mark_doc_superseded(
        "clickhouse",
        "ns",
        "kb://policy.md",
        embedding_model="text-embedding-3-small",
        ch=ch,
    )
    sql, params = ch.execute_calls[0]
    assert "INSERT INTO loom.chunk_supersessions" in sql
    assert "UPDATE" not in sql
    assert "SELECT DISTINCT org, namespace, embedding_model, source_uri, doc_version, now()" in sql
    assert "source_uri = {uri:String}" in sql and "embedding_model = {em:String}" in sql
    assert LIVE_CHUNK_SQL in sql  # only still-live ingestions are listed
    assert params["em"] == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_mark_doc_superseded_optional_filters_and_explicit_when() -> None:
    """knowledge.delete passes no embedding_model (cross-model supersede)
    and an explicit `when`. Both should thread through verbatim."""
    ch = StubCH()
    when = datetime(2026, 5, 17, 9, 30, tzinfo=UTC)
    await mark_doc_superseded("clickhouse", "ns", "kb://policy.md", when=when, ch=ch)
    sql, params = ch.execute_calls[0]
    assert "embedding_model = {em:String}" not in sql
    assert "customer_id = " not in sql
    assert "{when:DateTime}" in sql
    assert params["when"] == when


@pytest.mark.asyncio
async def test_count_active_for_doc_scopes_to_one_uri() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"n": 7})
    n = await count_active_for_doc("clickhouse", "ns", "kb://policy.md", ch=ch)
    sql, params = ch.query_one_calls[0]
    assert "source_uri = {uri:String}" in sql
    assert LIVE_CHUNK_SQL in sql
    assert params["uri"] == "kb://policy.md"
    assert n == 7


@pytest.mark.asyncio
async def test_count_active_by_uri_returns_per_uri_dict() -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {"source_uri": "kb://a.md", "n": 3},
            {"source_uri": "kb://b.md", "n": 1},
        ]
    )
    out = await count_active_by_uri(
        "clickhouse",
        "ns",
        ["kb://a.md", "kb://b.md", "kb://missing.md"],
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "source_uri IN {uris:Array(String)}" in sql
    assert "GROUP BY source_uri" in sql
    assert params["uris"] == ["kb://a.md", "kb://b.md", "kb://missing.md"]
    # URIs with zero active chunks are omitted from the dict (caller defaults).
    assert out == {"kb://a.md": 3, "kb://b.md": 1}


@pytest.mark.asyncio
async def test_count_active_by_uri_empty_input_skips_ch() -> None:
    ch = StubCH()
    out = await count_active_by_uri("clickhouse", "ns", [], ch=ch)
    assert out == {}
    assert ch.query_all_calls == []


@pytest.mark.asyncio
async def test_mark_docs_superseded_bulk_alter_with_when() -> None:
    ch = StubCH()
    when = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    await mark_docs_superseded(
        "clickhouse",
        "ns",
        ["kb://a.md", "kb://b.md"],
        when=when,
        ch=ch,
    )
    sql, params = ch.execute_calls[0]
    assert "source_uri IN {uris:Array(String)}" in sql
    assert "INSERT INTO loom.chunk_supersessions" in sql and "{when:DateTime}" in sql
    assert "UPDATE" not in sql
    assert params["uris"] == ["kb://a.md", "kb://b.md"]
    assert params["when"] == when


@pytest.mark.asyncio
async def test_mark_docs_superseded_empty_input_skips_ch() -> None:
    """Empty uri list short-circuits — no spurious ALTER UPDATE on
    an empty filter (which would mutate every row otherwise)."""
    ch = StubCH()
    await mark_docs_superseded("clickhouse", "ns", [], ch=ch)
    assert ch.execute_calls == []


@pytest.mark.asyncio
async def test_insert_chunks_forces_synchronous_visibility() -> None:
    ch = StubCH()
    n = await insert_chunks([_chunk_row(), _chunk_row()], ch=ch)
    assert n == 2
    table, _, settings = ch.insert_calls[0]
    assert table == "loom.chunks"
    # Search-visibility contract: async_insert=0 so the next search
    # sees the just-written chunks.
    assert settings.get("async_insert") == 0
