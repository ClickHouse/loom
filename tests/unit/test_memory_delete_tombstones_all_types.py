"""memory.delete tombstones the embeddings row of every memory type.

memory.set's single-write branch skips the embeddings row for episodic /
profile, but the bulk and extraction writers emit one for every row (the value
in `content_excerpt`, dim 0), so a delete gated on
`is_searchable_type(existing.memory_type)` would leave those rows live with
`tombstoned_at IS NULL` after their `memory` rows were tombstoned. Here: an episodic delete, a
profile delete and a semantic delete each write one tombstone row into the
embeddings table, carrying the columns of the live row it read; a key with
no embeddings row writes none; the embeddings failure stays non-fatal.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from server.memory.api.models_crud import MemoryDeleteInput
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.store import EMBEDDING_PERSISTED_COLS, MemoryRow

_NOW = datetime(2026, 9, 18, 12, 0, 0)


def _mk_row(**overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "mid-1",
        "org": "ops-org",
        "namespace": "ops-alerts",
        "agent": "sre-agent",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "alert:disk-full:host-7",
        "memory_type": "episodic",
        "value": {"state": "firing", "since": "2026-09-18T09:00:00Z"},
        "confidence": 1.0,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=1),
        "expires_at": None,
        "valid_at": None,
        "run_id": "",
        "tags": ["alert"],
        "entities": [],
        "temporal_anchor": None,
        "reinforcement_count": 0,
        "last_reinforced_at": None,
        "source_resource_id": "",
    }
    base.update(overrides)
    return MemoryRow(**base)


# What the argMax read returns for a bulk-written episodic key: the value as
# the excerpt, no vector (dim 0), no key vector.
_DIM0_EMB_ROW = {
    "embedding_model": "",
    "embedding_dim": 0,
    "embedding": [],
    "content_excerpt": '{"state": "firing", "since": "2026-09-18T09:00:00Z"}',
    "memory_key_embedding": [],
    "memory_key_embedding_dim": 0,
}

_EMBEDDED_ROW = {
    "embedding_model": "fake-test",
    "embedding_dim": 3,
    "embedding": [0.1, 0.1, 0.1],
    "content_excerpt": "disk full on host-7 (#alert:disk-full:host-7)",
    "memory_key_embedding": [0.2, 0.2, 0.2],
    "memory_key_embedding_dim": 3,
}


def _stored(emb: dict[str, Any], row: MemoryRow) -> dict[str, Any]:
    """The live embeddings row as `SELECT *` returns it for `row`'s key."""
    return {
        **emb,
        "org": row.org,
        "namespace": row.namespace,
        "agent": row.agent,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "memory_key": row.memory_key,
        "memory_type": row.memory_type,
        "tags": list(row.tags),
    }


class _StubCH:
    def __init__(
        self,
        *,
        emb_rows: list[dict[str, Any]] | None = None,
        fail_inserts_into: tuple[str, ...] = (),
    ) -> None:
        self.emb_rows = emb_rows or []
        self.fail_inserts_into = fail_inserts_into
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.query_all_sqls: list[str] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.query_all_sqls.append(sql)
        if "FROM loom.memory_embeddings" in sql:
            return [dict(r) for r in self.emb_rows]
        return []

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        if table in self.fail_inserts_into:
            raise RuntimeError(f"insert into {table} unavailable")
        self.insert_calls.append((table, rows, settings or {}))

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows, _ in self.insert_calls for r in rows if t == table]


async def _delete(monkeypatch: pytest.MonkeyPatch, existing: MemoryRow, ch: _StubCH) -> Any:
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    return await mem_handlers.handle_memory_delete(
        MemoryDeleteInput(
            org=existing.org,
            namespace=existing.namespace,
            agent=existing.agent,
            memory_key=existing.memory_key,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_type", ["episodic", "profile"])
async def test_non_searchable_delete_tombstones_the_dim0_embeddings_row(
    monkeypatch: pytest.MonkeyPatch, memory_type: str
) -> None:
    existing = _mk_row(memory_type=memory_type)
    ch = _StubCH(emb_rows=[_stored(_DIM0_EMB_ROW, existing)])
    out = await _delete(monkeypatch, existing, ch)
    assert out.tombstoned is True

    mem_rows = ch.rows_for("loom.memory")
    assert len(mem_rows) == 1 and mem_rows[0]["tombstoned_at"] is not None

    emb_rows = ch.rows_for("loom.memory_embeddings")
    assert len(emb_rows) == 1, "the embeddings row must be tombstoned for every type"
    row = emb_rows[0]
    assert set(EMBEDDING_PERSISTED_COLS).issubset(row.keys())
    assert row["tombstoned_at"] is not None
    assert row["memory_type"] == memory_type
    # Mirrors what the table held: no vector, the value as the excerpt, so the
    # tombstone collapses onto the original row under the RMT sort key.
    assert row["embedding_dim"] == 0
    assert row["embedding"] == []
    assert row["content_excerpt"] == _DIM0_EMB_ROW["content_excerpt"]
    assert row["memory_key"] == "alert:disk-full:host-7"
    # Sync insert: a delete is a correctness path.
    _, _, settings = next(c for c in ch.insert_calls if c[0] == "loom.memory_embeddings")
    assert settings.get("async_insert") == 0


@pytest.mark.asyncio
async def test_embedded_episodic_delete_carries_the_vector_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An embedded episodic row: the tombstone reuses the
    stored vector (no re-embed) so it shares the sort key with the original."""
    existing = _mk_row(memory_type="episodic")
    ch = _StubCH(emb_rows=[_stored(_EMBEDDED_ROW, existing)])
    out = await _delete(monkeypatch, existing, ch)
    assert out.tombstoned is True
    emb_rows = ch.rows_for("loom.memory_embeddings")
    assert len(emb_rows) == 1
    assert emb_rows[0]["tombstoned_at"] is not None
    assert emb_rows[0]["embedding"] == [0.1, 0.1, 0.1]
    assert emb_rows[0]["embedding_dim"] == 3
    assert emb_rows[0]["memory_key_embedding"] == [0.2, 0.2, 0.2]


@pytest.mark.asyncio
async def test_semantic_delete_still_tombstones(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _mk_row(memory_type="semantic")
    ch = _StubCH(emb_rows=[_stored(_EMBEDDED_ROW, existing)])
    out = await _delete(monkeypatch, existing, ch)
    assert out.tombstoned is True
    emb_rows = ch.rows_for("loom.memory_embeddings")
    assert len(emb_rows) == 1
    assert emb_rows[0]["tombstoned_at"] is not None
    assert emb_rows[0]["memory_type"] == "semantic"


@pytest.mark.asyncio
async def test_key_without_an_embeddings_row_writes_no_tombstone_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key memory.set wrote through its single-write branch has no
    embeddings row; the read finds nothing and nothing is inserted — the
    guard drop costs one primary-key read, never a stray row."""
    ch = _StubCH(emb_rows=[])
    out = await _delete(monkeypatch, _mk_row(memory_type="episodic"), ch)
    assert out.tombstoned is True
    assert any("FROM loom.memory_embeddings" in s for s in ch.query_all_sqls)
    assert ch.rows_for("loom.memory_embeddings") == []
    assert len(ch.rows_for("loom.memory")) == 1


@pytest.mark.asyncio
async def test_embeddings_failure_stays_non_fatal_for_non_searchable_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _mk_row(memory_type="episodic")
    ch = _StubCH(
        emb_rows=[_stored(_DIM0_EMB_ROW, existing)], fail_inserts_into=("loom.memory_embeddings",)
    )
    out = await _delete(monkeypatch, existing, ch)
    assert out.tombstoned is True  # the memory tombstone committed
    assert len(ch.rows_for("loom.memory")) == 1
