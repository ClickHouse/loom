"""A fact's stated end date reaches the row.

The extraction prompt asks for `valid_to` ("worked at Acme until 2024"), the
pipeline parses it into the bulk entry, and the bulk handler's row dict must
carry it — the edge and triple legs filter on it, so a dropped `valid_to`
silently leaves every row open-ended. These tests pin
the two write sites (memory row, embedding row) and the read-side predicate.

CH-free: stub clients capture inserts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app import session_summary as _session_summary_mod
from server.memory.app.extract import handler as mem_extract

END = datetime(2024, 1, 1)


class _StubCH:
    def __init__(self) -> None:
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.insert_calls.append((table, rows, settings or {}))


class _StubEmbedder:
    model_name = "fake-test"
    dim = 3

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    embedding_rows: list[dict[str, Any]] = []

    def make(ch: _StubCH) -> list[dict[str, Any]]:
        monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
        monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
        monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: _StubEmbedder())
        monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: _StubEmbedder())

        async def _none(*_: Any, **__: Any) -> Any:
            return None

        async def _empty(*_: Any, **__: Any) -> dict[str, str]:
            return {}

        async def _capture_embeddings(rows: list[dict[str, Any]], *a: Any, **kw: Any) -> None:
            embedding_rows.extend(rows)

        monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _none)
        monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _none)
        monkeypatch.setattr(
            "server.memory.app.entity_resolution.resolve_entities_for_write", _empty
        )
        monkeypatch.setattr("server.memory.store.insert_embedding_rows", _capture_embeddings)
        monkeypatch.setattr("server.memory.store.usage.record_usage_event", _none)
        for name in ("write_entity_vectors", "write_entity_edges", "write_entity_triples"):
            monkeypatch.setattr(_bulk_set_mod, name, _none)
        return embedding_rows

    return make


def _input(**entry_kwargs: Any) -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="test",
        namespace="ns",
        agent="a",
        user_id="u",
        session_id="s1",
        entries=[
            _BulkSetEntry(
                memory_key="user_employer_acme",
                memory_type="semantic",
                value="User worked at Acme from 2022 to 2024.",
                **entry_kwargs,
            )
        ],
    )


def _memory_rows(ch: _StubCH) -> list[dict[str, Any]]:
    return [r for table, rows, _ in ch.insert_calls if table == "loom.memory" for r in rows]


@pytest.mark.asyncio
async def test_bulk_set_writes_valid_to_on_memory_and_embedding_rows(harness) -> None:
    ch = _StubCH()
    embedding_rows = harness(ch)
    await mem_extract.handle_memory_bulk_set(_input(valid_at=datetime(2022, 1, 1), valid_to=END))
    rows = _memory_rows(ch)
    assert len(rows) == 1 and rows[0]["valid_to"] == END
    assert rows[0]["valid_at"] == datetime(2022, 1, 1)
    assert len(embedding_rows) == 1 and embedding_rows[0]["valid_to"] == END


@pytest.mark.asyncio
async def test_bulk_set_open_interval_is_explicit_null(harness) -> None:
    """The column is on every row (RMT FINAL: an omitted column zeroes)."""
    ch = _StubCH()
    embedding_rows = harness(ch)
    await mem_extract.handle_memory_bulk_set(_input())
    rows = _memory_rows(ch)
    assert "valid_to" in rows[0] and rows[0]["valid_to"] is None
    assert "valid_to" in embedding_rows[0] and embedding_rows[0]["valid_to"] is None


def test_point_set_input_accepts_valid_to() -> None:
    from server.memory.api.models_crud import MemorySetInput

    inp = MemorySetInput(
        org="o",
        namespace="n",
        agent="a",
        memory_key="k",
        value="v",
        valid_to=END,
    )
    assert inp.valid_to == END


def test_search_where_drops_a_fact_past_its_valid_to() -> None:
    from server.memory.domain.search_sql import SearchQuery, _build_where

    base: dict[str, Any] = dict(
        org="o",
        namespace="n",
        agent="a",
        scope="private",
        embedding_model="m",
        query_vec=[0.0],
        top_k=5,
    )
    live, _ = _build_where(SearchQuery(**base))
    assert any("e.valid_to IS NULL OR e.valid_to > now()" in w for w in live)
