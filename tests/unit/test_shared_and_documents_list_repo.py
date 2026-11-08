"""Tests for shared_repo and documents_repo.list_with_chunk_counts.

shared_repo backs memory.promote / memory.demote. Its insert + fetch
shape is the loom.shared lifecycle contract; the embedding-side
tombstone is what makes memory.search reflect a demote.

list_with_chunk_counts is the JOIN under knowledge.list — the most
complex documents-side query.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.core.tag_sql import TagFilter, tag_where
from server.knowledge.store import documents as documents_repo
from server.memory.store import shared as shared_repo

# The any-case tag matches, as the list reads render them.
_FOLDED_ANY = tag_where(TagFilter(("x",), "folded_any"), {})
_FOLDED_ALL = tag_where(TagFilter(("x",), "folded_all"), {})


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


# ----------------------------------------------------------------------
# shared_repo
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_shared_row_forces_synchronous_visibility() -> None:
    ch = StubCH()
    await shared_repo.insert_shared_row(
        {
            **dict.fromkeys(shared_repo.SHARED_PERSISTED_COLS),
            "org": "clickhouse",
            "namespace": "ns",
            "memory_key": "k",
        },
        ch=ch,
    )
    table, _, settings = ch.insert_calls[0]
    assert table == "loom.shared"
    # search-visibility contract: next FINAL read sees the row.
    assert settings.get("async_insert") == 0


@pytest.mark.asyncio
async def test_insert_shared_row_refuses_a_partial_or_misspelled_row() -> None:
    """A version row short of a column would let FINAL serve that column's
    DDL default over the promotion's value; nothing is written."""
    ch = StubCH()
    row = dict.fromkeys(shared_repo.SHARED_PERSISTED_COLS)
    del row["promoted_from_candidate_id"]
    with pytest.raises(ValueError, match=r"missing=\['promoted_from_candidate_id'\]"):
        await shared_repo.insert_shared_row(row, ch=ch)
    with pytest.raises(ValueError, match=r"unknown=\['rationle'\]"):
        await shared_repo.insert_shared_row(
            {**dict.fromkeys(shared_repo.SHARED_PERSISTED_COLS), "rationle": ""}, ch=ch
        )
    assert ch.insert_calls == []


def test_shared_persisted_cols_are_the_schema_columns() -> None:
    from tests.unit.schema_ddl import column_names

    assert list(shared_repo.SHARED_PERSISTED_COLS) == column_names("shared", materialized=False)


@pytest.mark.asyncio
async def test_insert_shared_embedding_uses_memory_embeddings_table() -> None:
    """The shared embedding lives on loom.memory_embeddings (the same
    table as private embeddings) — scope='shared' is the marker."""
    ch = StubCH()
    await shared_repo.insert_shared_embedding(
        {"scope": "shared", "agent": "_shared"},
        ch=ch,
    )
    table, rows, _ = ch.insert_calls[0]
    assert table == "loom.memory_embeddings"
    assert rows[0]["agent"] == "_shared"


@pytest.mark.asyncio
async def test_insert_shared_embedding_writes_a_complete_builder_row() -> None:
    """The shared embedding goes through build_embedding_row like every other
    embeddings write: every persisted column present, the app-side lemma
    column filled (else the lemma lexical leg never finds a promoted row),
    and an RMT version stamped."""
    from server.memory.store.rows import EMBEDDING_PERSISTED_COLS

    ch = StubCH()
    await shared_repo.insert_shared_embedding(
        {"scope": "shared", "agent": "_shared", "content_excerpt": "dogs were barking"},
        ch=ch,
    )
    row = ch.insert_calls[0][1][0]
    assert set(row) == set(EMBEDDING_PERSISTED_COLS)
    assert row["content_excerpt_lemma"] == "dog be bark"
    assert row["version"] > 0
    assert row["created_at"] is not None


@pytest.mark.asyncio
async def test_promote_carries_the_source_provenance_onto_the_shared_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source_class and a live attestation ride onto the shared embedding, so
    the provenance ranking term and memory.ask's unverified-external filter
    treat a promoted row like its source; a stale attestation does not count."""
    from types import SimpleNamespace

    from server.memory import store as memory_repo
    from server.memory.api.models_lifecycle import MemoryPromoteInput
    from server.memory.app import conflicts
    from server.memory.app import promote as _memory_promote

    written: list[dict[str, Any]] = []

    class _Emb:
        model_name = "fake"
        dim = 2

        async def embed(self, text: str) -> list[float]:
            return [0.1, 0.2]

    async def _no_peers(**kw: Any) -> list[Any]:
        return []

    async def _shared_row(row: dict[str, Any], **kw: Any) -> None:
        return None

    async def _shared_emb(row: dict[str, Any], **kw: Any) -> None:
        written.append(row)

    monkeypatch.setattr(_memory_promote, "get_embedder", lambda: _Emb())
    monkeypatch.setattr(conflicts, "check_against_shared", _no_peers)
    monkeypatch.setattr(shared_repo, "insert_shared_row", _shared_row)
    monkeypatch.setattr(shared_repo, "insert_shared_embedding", _shared_emb)

    for verified_sha, want in (("a" * 64, 1), ("b" * 64, 0)):
        src = SimpleNamespace(
            namespace="team",
            agent="a",
            user_id="-",
            session_id="-",
            memory_key="k",
            value={"v": "external fact"},
            memory_type="semantic",
            confidence=0.9,
            valid_at=None,
            source_class="external",
            verified_by="human:alice",
            verified_sha256=verified_sha,
            content_sha256="a" * 64,
        )

        async def _get(*a: Any, _src: Any = src, **kw: Any) -> Any:
            return _src

        monkeypatch.setattr(memory_repo, "get_by_key", _get)
        written.clear()
        await _memory_promote.handle_memory_promote(
            MemoryPromoteInput(org="o", namespace="n", agent="a", memory_key="k")
        )
        assert written[0]["source_class"] == "external"
        assert written[0]["verified"] == want


@pytest.mark.asyncio
async def test_fetch_shared_returns_dict_or_none() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"value": "v", "memory_type": "preference", "tombstoned_at": None})
    out = await shared_repo.fetch_shared("clickhouse", "ns", "k", ch=ch)
    assert out is not None
    sql, _ = ch.query_one_calls[0]
    assert "FROM loom.shared FINAL" in sql
    assert "memory_key = {memory_key:String}" in sql


@pytest.mark.asyncio
async def test_fetch_shared_none_when_missing() -> None:
    ch = StubCH()
    ch.query_one_returns.append(None)
    out = await shared_repo.fetch_shared("clickhouse", "ns", "missing", ch=ch)
    assert out is None


@pytest.mark.asyncio
async def test_tombstone_shared_embedding_re_emits_a_versioned_row() -> None:
    """memory.search(scope='shared') filters on tombstoned_at IS NULL; the
    demote path lands the tombstone as a committed re-emit of the live shared
    rows (every column carried, tombstoned_at set, fresh version) so the next
    search does not see the row — and never as an ALTER UPDATE, whose patch
    part would cost the fact leg its codes-first read."""
    from server.memory.store import EMBEDDING_PERSISTED_COLS

    ch = StubCH()
    when = datetime(2026, 5, 17, tzinfo=UTC)
    ch.query_all_returns.append(
        [
            {
                "org": "clickhouse",
                "namespace": "ns",
                "agent": "_shared",
                "scope": "shared",
                "memory_key": "k",
                "embedding_model": "m",
                "embedding_dim": 3,
                "embedding": [0.1, 0.2, 0.3],
                "content_excerpt": "shared text",
                "version": 7,
            }
        ]
    )
    await shared_repo.tombstone_shared_embedding("clickhouse", "ns", "k", when, version=9, ch=ch)
    assert ch.execute_calls == []
    read_sql, read_params = ch.query_all_calls[0]
    assert "scope = 'shared'" in read_sql and "tombstoned_at IS NULL" in read_sql
    assert "FINAL" in read_sql and read_params["k"] == "k"
    ((table, rows, settings),) = ch.insert_calls
    assert table == "loom.memory_embeddings" and settings == {
        "async_insert": 0,
        "wait_for_async_insert": 1,
    }
    (row,) = rows
    assert set(row) >= set(EMBEDDING_PERSISTED_COLS)
    assert row["tombstoned_at"] == when and row["scope"] == "shared"
    assert row["embedding"] == [0.1, 0.2, 0.3] and row["version"] == 9  # the demote's


@pytest.mark.asyncio
async def test_tombstone_shared_embedding_with_no_live_row_writes_nothing() -> None:
    ch = StubCH()
    await shared_repo.tombstone_shared_embedding(
        "clickhouse", "ns", "k", datetime(2026, 5, 17, tzinfo=UTC), version=9, ch=ch
    )
    assert ch.insert_calls == [] and ch.execute_calls == []


# ----------------------------------------------------------------------
# documents_repo.list_with_chunk_counts
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_with_chunk_counts_default_excludes_superseded() -> None:
    """knowledge.list returns live docs only by default. The handler
    can opt-in via include_superseded=True."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await documents_repo.list_with_chunk_counts("clickhouse", "ns", ch=ch)
    sql, params = ch.query_all_calls[0]
    # Predicates filter the inner FINAL subquery (bare, no d. prefix); the
    # outer query keeps the d. alias for the join + ordering.
    assert "superseded_at IS NULL" in sql
    assert "LEFT JOIN" in sql
    assert "ORDER BY d.ingested_at DESC, d.source_uri DESC" in sql
    assert params["lim"] == 50
    # the chunk counts are read for this namespace only, not every tenant's
    counts = sql.split("LEFT JOIN", 1)[1]
    assert (
        "FROM loom.chunks\n            WHERE org = {org:String} AND namespace = {ns:String}"
        in counts
    )


@pytest.mark.asyncio
async def test_list_with_chunk_counts_include_superseded_drops_filter() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await documents_repo.list_with_chunk_counts(
        "clickhouse",
        "ns",
        include_superseded=True,
        ch=ch,
    )
    sql, _ = ch.query_all_calls[0]
    # The documents filter is dropped; the chunk-count subquery still carries
    # its own `doc_superseded_at IS NULL`, so match the doc filter precisely.
    assert "AND superseded_at IS NULL" not in sql


@pytest.mark.asyncio
async def test_list_with_chunk_counts_cursor_strips_tz_for_ch() -> None:
    """ClickHouse params expect naive datetimes (the column is
    DateTime, not DateTime64-tz). A tz-aware cursor must be converted
    to UTC-naive before it is bound."""
    ch = StubCH()
    ch.query_all_returns.append([])
    cursor = (datetime(2026, 5, 17, 12, 0, tzinfo=UTC), "kb://policy.md")
    await documents_repo.list_with_chunk_counts(
        "clickhouse",
        "ns",
        cursor=cursor,
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "(toDateTime({cur_ts:String}, 'UTC'), {cur_uri:String})" in sql
    # Both cursor values are bound parameters, never text in the statement.
    assert params["cur_ts"] == "2026-05-17 12:00:00" and "2026-05-17" not in sql
    assert params["cur_uri"] == "kb://policy.md"


@pytest.mark.asyncio
async def test_list_with_chunk_counts_matches_tags_in_any_case() -> None:
    """The folded predicate, with the asked tags bound as typed."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await documents_repo.list_with_chunk_counts(
        "clickhouse", "ns", tag_filter=TagFilter(("Specs",), "folded_any"), ch=ch
    )
    sql, params = ch.query_all_calls[0]
    assert _FOLDED_ANY in sql
    assert "hasAny(tags, {tags:Array(String)})" not in sql
    assert params["tags"] == ["Specs"]


@pytest.mark.asyncio
async def test_list_with_chunk_counts_requires_every_tag_when_asked() -> None:
    """`tags_every=True` needs every tag: the folded predicate with `hasAll`."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await documents_repo.list_with_chunk_counts(
        "clickhouse", "ns", tag_filter=TagFilter(("Specs", "onboarding"), "folded_all"), ch=ch
    )
    sql, params = ch.query_all_calls[0]
    assert _FOLDED_ALL in sql
    assert params["tags"] == ["Specs", "onboarding"]
