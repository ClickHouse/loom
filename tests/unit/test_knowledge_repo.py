"""Unit tests for server/knowledge/store/chunks.py — mocked CH client."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from server.core import rmt_carry
from server.core.ch import _FENCE_MAX_S, DEADLINE_MESSAGE
from server.core.errors import BackendUnavailableError
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
from server.knowledge.store.chunks import (
    ChunkRow,
    count_active_by_uri,
    count_active_for_doc,
    insert_chunks,
    list_for_doc,
    mark_doc_superseded,
    mark_docs_superseded,
    retire_versions,
)
from server.settings import settings


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.settings_seen: list[dict[str, Any]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql, params=None, settings=None):
        self.execute_calls.append((sql, params or {}))
        if settings is not None:
            self.settings_seen.append(settings)

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
    """One statement retires, per source, the chunk versions older than its own bound."""
    ch = StubCH()
    when = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    await mark_docs_superseded(
        "clickhouse",
        "ns",
        {"kb://b.md": 9, "kb://a.md": 7},
        when=when,
        ch=ch,
    )
    sql, params = ch.execute_calls[0]
    assert "source_uri IN {uris:Array(String)}" in sql
    assert "doc_version < arrayElement({befores:Array(UInt64)}" in sql
    assert "INSERT INTO loom.chunk_supersessions" in sql and "{when:DateTime}" in sql
    assert "UPDATE" not in sql
    assert (params["uris"], params["befores"]) == (["kb://a.md", "kb://b.md"], [7, 9])
    assert params["when"] == when


@pytest.mark.asyncio
async def test_mark_docs_superseded_empty_input_skips_ch() -> None:
    """Empty uri list short-circuits — no spurious ALTER UPDATE on
    an empty filter (which would mutate every row otherwise)."""
    ch = StubCH()
    await mark_docs_superseded("clickhouse", "ns", {}, ch=ch)
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


@pytest.mark.asyncio
async def test_retire_versions_writes_its_rows_from_the_pairs_not_from_the_chunks() -> None:
    """A chunk insert that timed out may land after the cleanup: rows written from the
    pairs retire it then too, where a SELECT over loom.chunks would find nothing yet."""

    ch = StubCH()
    when = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    await retire_versions("o", "ns", [("kb://a.md", 7)], embedding_model="m", when=when, ch=ch)
    assert ch.execute_calls == []
    table, rows, _ = ch.insert_calls[0]
    assert table == "loom.chunk_supersessions"
    assert rows == [
        {
            "org": "o",
            "namespace": "ns",
            "embedding_model": "m",
            "source_uri": "kb://a.md",
            "doc_version": 7,
            "superseded_at": when,
        }
    ]


@pytest.mark.asyncio
async def test_a_retirement_sent_under_a_lease_starts_by_its_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a publication's lease the retirement carries the lease's start guard, as the
    lease's inserts do, and runs no longer than the write limit: one that would start
    after the lease an erasure or a restore waited out is refused, and its refusal
    answers as a late send. A write class timeout of 0 (no class deadline) still bounds
    it."""

    monkeypatch.setattr(settings, "ch_timeout_write_s", 0)

    async def deadline(ch: Any) -> float:
        return 1_700_000_000.25

    monkeypatch.setattr(rmt_carry, "measured_send_deadline", deadline)
    ch = StubCH()
    with rmt_carry.under_lease(time.monotonic() + 30):
        await mark_docs_superseded("o", "ns", {"kb://a.md": 7}, ch=ch)
    sql, params = ch.execute_calls[0]
    assert "throwIf(toUnixTimestamp64Milli(now64(3)) > {send_by_ms:Int64}" in sql
    assert params["send_by_ms"] == 1_700_000_000_250
    # Bounded by what an erasure waits for, though the write class sets no deadline.
    assert ch.settings_seen == [{"max_execution_time": _FENCE_MAX_S}]

    class _Late(StubCH):
        async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
            raise RuntimeError(f"Code: 395. DB::Exception: {DEADLINE_MESSAGE}")

    with (
        rmt_carry.under_lease(time.monotonic() + 30),
        pytest.raises(BackendUnavailableError, match="too late"),
    ):
        await mark_docs_superseded("o", "ns", {"kb://a.md": 7}, ch=_Late())
    unguarded = StubCH()
    await mark_docs_superseded("o", "ns", {"kb://a.md": 7}, ch=unguarded)
    assert "send_by_ms" not in unguarded.execute_calls[0][1]
