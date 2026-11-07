"""Secondary write paths must mirror every embeddings column; touch mirrors expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_crud import (
    MemoryDeleteInput,
    MemoryRestoreInput,
    MemoryTouchInput,
    MemoryUpdateByIdInput,
)
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import restore as restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.app import update as update_mod
from server.memory.store import EMBEDDING_PERSISTED_COLS, MemoryRow

_NOW = datetime(2026, 6, 10, 12, 0, 0)


def _mk_row(**overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "mid-123",
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "user_dog_name",
        "memory_type": "semantic",
        "value": "User's dog is named Biscuit",
        "confidence": 0.8,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=3),
        "expires_at": None,
        "valid_at": datetime(2025, 3, 1),
        "run_id": "run-7",
        "tags": ["pets", "verify"],
        "entities": ["biscuit"],
        "temporal_anchor": datetime(2025, 3, 2),
        "reinforcement_count": 3,
        "last_reinforced_at": _NOW - timedelta(days=1),
        "source_resource_id": "res-1",
    }
    base.update(overrides)
    return MemoryRow(**base)


class _StubCH:
    def __init__(
        self,
        *,
        query_one_result: dict[str, Any] | None = None,
        query_all_routes: list[tuple[str, list[dict[str, Any]]]] | None = None,
        fail_lightweight_update: bool = False,
        fail_all_updates: bool = False,
        fail_inserts_into: tuple[str, ...] = (),
    ) -> None:
        self.query_one_result = query_one_result
        self.query_all_routes = query_all_routes or []
        self.fail_lightweight_update = fail_lightweight_update
        self.fail_all_updates = fail_all_updates
        self.fail_inserts_into = fail_inserts_into
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_sqls: list[str] = []

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        return self.query_one_result

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.query_all_sqls.append(sql)
        for needle, result in self.query_all_routes:
            if needle in sql:
                return result
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

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        if self.fail_all_updates:
            raise RuntimeError("UPDATE unavailable")
        if self.fail_lightweight_update and sql.lstrip().startswith("UPDATE"):
            raise RuntimeError("lightweight UPDATE not enabled")
        self.execute_calls.append((sql, params or {}))

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows, _ in self.insert_calls for r in rows if t == table]


class _RecordingEmbedder:
    model_name = "fake-test"
    dim = 3

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(self.calls))] * 3


_EMB_TABLE_ROW = {
    "embedding_model": "fake-test",
    "embedding_dim": 3,
    "embedding": [0.1, 0.1, 0.1],
    "content_excerpt": "User's dog is named Biscuit (#user_dog_name)",
    "memory_key_embedding": [0.2, 0.2, 0.2],
    "memory_key_embedding_dim": 3,
}


@pytest.mark.asyncio
async def test_restore_mirrors_every_embedding_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.claims_fake import fake_key_locks

    fake_key_locks(monkeypatch)  # an Undo takes the key's write lock
    tombstoned_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    ch = _StubCH(
        query_one_result={
            "memory_key": "user_dog_name",
            "memory_type": "semantic",
            "value": '"User\'s dog is named Biscuit"',
            "confidence": 0.8,
            "source_trace": "",
            "memory_id": "mid-123",
            "entities": ["biscuit"],
            "tags": ["pets", "verify"],
            "valid_at": datetime(2025, 3, 1),
            "char_limit": None,
            "pinned": 0,
            "tombstoned_at": tombstoned_at,
            "version": 100,
        },
        # restore reads the key dim under its own alias
        query_all_routes=[
            ("FROM loom.memory_embeddings", [dict(_EMB_TABLE_ROW) | {"key_embedding_dim": 3}])
        ],
    )
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    existing = _mk_row(tombstoned_at=tombstoned_at, source_class="extracted", valid_to=_NOW)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)

    out = await restore_mod.handle_memory_restore(
        MemoryRestoreInput(
            org="test",
            namespace="personal",
            agent="cursor",
            memory_key="user_dog_name",
        )
    )
    assert out.restored is True

    emb_rows = ch.rows_for("loom.memory_embeddings")
    assert len(emb_rows) == 1
    row = emb_rows[0]
    assert set(EMBEDDING_PERSISTED_COLS).issubset(row.keys())
    assert row["memory_key_embedding"] == [0.2, 0.2, 0.2]
    assert row["memory_key_embedding_dim"] == 3
    assert row["tags"] == ["pets", "verify"]
    assert row["entities"] == ["biscuit"]
    assert row["confidence"] == pytest.approx(0.8)
    assert row["valid_at"] == datetime(2025, 3, 1)
    assert row["temporal_anchor"] == datetime(2025, 3, 2)
    assert row["reinforcement_count"] == 3
    assert row["run_id"] == "run-7"
    assert row["source_resource_id"] == "res-1"
    assert row["tombstoned_at"] is None
    assert row["expires_at"] is None
    # The stored excerpt comes back as it was, not rebuilt; provenance and validity with it.
    assert row["content_excerpt"] == _EMB_TABLE_ROW["content_excerpt"]
    assert row["source_class"] == "extracted"
    assert row["valid_to"] == _NOW
    assert row["verified"] == 0
    emb_sql = next(s for s in ch.query_all_sqls if "loom.memory_embeddings" in s)
    assert "argMaxIf(memory_key_embedding" in emb_sql
    assert "argMax(content_excerpt, version)" in emb_sql


@pytest.mark.asyncio
async def test_delete_tombstone_carries_vectors_and_signals_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live row as `SELECT *` returns it: the tombstone carries the table's own columns.
    stored = {
        **_EMB_TABLE_ROW,
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "user_dog_name",
        "memory_type": "semantic",
        "tags": ["pets", "verify"],
        "reinforcement_count": 3,
        "version": 100,
    }
    ch = _StubCH(query_all_routes=[("FROM loom.memory_embeddings", [stored])])
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    existing = _mk_row()

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)

    out = await mem_handlers.handle_memory_delete(
        MemoryDeleteInput(
            org="test",
            namespace="personal",
            agent="cursor",
            memory_key="user_dog_name",
        )
    )
    assert out.tombstoned is True

    emb_rows = ch.rows_for("loom.memory_embeddings")
    assert len(emb_rows) == 1
    row = emb_rows[0]
    assert set(EMBEDDING_PERSISTED_COLS).issubset(row.keys())
    assert row["tombstoned_at"] is not None
    assert row["embedding"] == [0.1, 0.1, 0.1]
    assert row["memory_key_embedding"] == [0.2, 0.2, 0.2]
    assert row["memory_key_embedding_dim"] == 3
    assert row["content_excerpt"].endswith("(#user_dog_name)")
    assert row["tags"] == ["pets", "verify"]
    assert row["reinforcement_count"] == 3
    assert row["version"] > 100


@pytest.mark.asyncio
async def test_touch_mirrors_expiry_onto_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expiry mirror is a versioned re-emit of the key's
    live embedding rows — every persisted column and both vectors carried,
    `expires_at` set, a fresh version — never a lightweight UPDATE (its patch
    part would switch the fact leg's codes-first read off)."""
    ch = _StubCH(query_all_routes=[("FROM loom.memory_embeddings", [dict(_EMB_TABLE_ROW)])])
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return _mk_row()

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)

    out = await mem_handlers.handle_memory_touch(
        MemoryTouchInput(
            org="test",
            namespace="personal",
            agent="cursor",
            memory_key="user_dog_name",
            ttl_seconds=7200,
        )
    )
    assert out.touched is True
    mem_rows = ch.rows_for("loom.memory")
    assert mem_rows and mem_rows[0]["expires_at"] is not None
    assert ch.execute_calls == []  # no UPDATE, lightweight or ALTER
    emb_rows = ch.rows_for("loom.memory_embeddings")
    assert len(emb_rows) == 1
    row = emb_rows[0]
    assert row["expires_at"] == out.expires_at
    assert set(row) >= set(EMBEDDING_PERSISTED_COLS)
    assert row["embedding"] == _EMB_TABLE_ROW["embedding"]
    assert row["memory_key_embedding"] == _EMB_TABLE_ROW["memory_key_embedding"]
    assert row["content_excerpt"] == _EMB_TABLE_ROW["content_excerpt"]
    assert row["version"] > 0
    # the read is one keyed FINAL statement over the full identity
    read_sql = next(s for s in ch.query_all_sqls if "loom.memory_embeddings" in s)
    assert "FINAL" in read_sql and "memory_key = {k:String}" in read_sql


@pytest.mark.asyncio
async def test_touch_re_emit_skips_a_key_with_no_live_embedding_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key whose embedding rows are all tombstoned (or never embedded)
    gets no re-emit: nothing to carry forward, nothing to UPDATE."""
    ch = _StubCH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return _mk_row()

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)

    out = await mem_handlers.handle_memory_touch(
        MemoryTouchInput(
            org="test",
            namespace="personal",
            agent="cursor",
            memory_key="user_dog_name",
            ttl_seconds=60,
        )
    )
    assert out.touched is True
    assert ch.rows_for("loom.memory_embeddings") == []
    assert ch.execute_calls == []


@pytest.mark.asyncio
async def test_touch_survives_total_mirror_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _StubCH(
        query_all_routes=[("FROM loom.memory_embeddings", [dict(_EMB_TABLE_ROW)])],
        fail_inserts_into=("loom.memory_embeddings",),
    )
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return _mk_row()

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)

    out = await mem_handlers.handle_memory_touch(
        MemoryTouchInput(
            org="test",
            namespace="personal",
            agent="cursor",
            memory_key="user_dog_name",
            ttl_seconds=60,
        )
    )
    assert out.touched is True
    assert ch.rows_for("loom.memory")


def _patch_update_by_id_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ch: _StubCH,
    embedder: _RecordingEmbedder,
    blacklist_calls: list[str],
    blacklist_raises: bool = False,
    existing: MemoryRow | None = None,
) -> None:
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr(mem_handlers, "get_embedder", lambda: embedder)
    monkeypatch.setattr("server.memory.app.set_commit.get_embedder", mem_handlers.get_embedder)
    monkeypatch.setattr("server.memory.app.update.get_embedder", mem_handlers.get_embedder)

    async def _get_by_id(*_: Any, **__: Any) -> MemoryRow:
        return existing or _mk_row()

    monkeypatch.setattr("server.memory.store.get_by_id", _get_by_id)

    async def _namespaces_of_id(*_: Any, **__: Any) -> list[str]:
        return [(existing or _mk_row()).namespace]

    monkeypatch.setattr("server.memory.store.namespaces_of_id", _namespaces_of_id)

    async def _enforce(*, org: str, namespace: str, value: str, policy: object = None) -> None:
        assert policy is None  # the ClickHouse authority has no policy generations
        blacklist_calls.append(value)
        if blacklist_raises:
            raise HTTPException(status_code=422, detail={"code": "BLACKLISTED"})

    monkeypatch.setattr("server.memory.app.blacklist_guard.enforce_on_insert", _enforce)

    # The shared write path checks the new value for conflicts
    # and runs same-key supersession; both need ClickHouse, so keep them inert.
    from server.memory.app import conflicts as _cf
    from server.memory.app import key_supersession as _ks
    from server.memory.domain.conflicts import ConflictResult as _CR

    async def _clear(**_: Any) -> _CR:
        return _CR(verdict="clear", peers=[])

    async def _zero(**_: Any) -> int:
        return 0

    monkeypatch.setattr(_cf, "check_on_commit", _clear)
    monkeypatch.setattr(_ks, "supersede_same_key_rows", _zero)


@pytest.mark.asyncio
async def test_update_by_id_rejects_blacklisted_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _StubCH()
    blacklist_calls: list[str] = []
    _patch_update_by_id_deps(
        monkeypatch,
        ch=ch,
        embedder=_RecordingEmbedder(),
        blacklist_calls=blacklist_calls,
        blacklist_raises=True,
    )
    with pytest.raises(HTTPException) as exc:
        await update_mod.handle_memory_update_by_id(
            MemoryUpdateByIdInput(
                org="test",
                memory_id="mid-123",
                value="now contains FORBIDDEN_TOKEN_XYZ",
            )
        )
    assert exc.value.status_code == 422
    assert blacklist_calls == ['"now contains FORBIDDEN_TOKEN_XYZ"']
    assert ch.insert_calls == []


@pytest.mark.asyncio
async def test_update_by_id_preserves_memory_id_and_humanizes_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _StubCH()
    embedder = _RecordingEmbedder()
    blacklist_calls: list[str] = []
    _patch_update_by_id_deps(
        monkeypatch,
        ch=ch,
        embedder=embedder,
        blacklist_calls=blacklist_calls,
    )
    out = await update_mod.handle_memory_update_by_id(
        MemoryUpdateByIdInput(
            org="test",
            memory_id="mid-123",
            value="Dog is now named Bowie",
        )
    )
    assert out.updated is True
    assert blacklist_calls

    mem_rows = ch.rows_for("loom.memory")
    assert len(mem_rows) == 1
    assert mem_rows[0]["memory_id"] == "mid-123"

    assert "user dog name" in embedder.calls
    assert "user_dog_name" not in embedder.calls

    emb_rows = ch.rows_for("loom.memory_embeddings")
    assert len(emb_rows) == 1
    assert emb_rows[0]["memory_key_embedding"] == [2.0, 2.0, 2.0]
    assert emb_rows[0]["memory_key_embedding_dim"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "redacted"), [("Dog is now named Bowie", False), (None, True)])
async def test_update_by_id_clears_a_redaction_only_with_a_new_value(
    monkeypatch: pytest.MonkeyPatch, value: str | None, redacted: bool
) -> None:
    """A new value is not the redacted text, so its version is not marked redacted; a
    rewrite that keeps the value keeps the mark."""
    ch = _StubCH()
    scrubbed = _mk_row(value="[REDACTED]", redacted_at=_NOW, redaction_reason="pii|admin")
    _patch_update_by_id_deps(
        monkeypatch, ch=ch, embedder=_RecordingEmbedder(), blacklist_calls=[], existing=scrubbed
    )
    out = await update_mod.handle_memory_update_by_id(
        MemoryUpdateByIdInput(org="test", memory_id="mid-123", value=value, tags=["kept"])
    )
    assert out.updated is True
    [row] = ch.rows_for("loom.memory")
    assert (row["redacted_at"] is not None, bool(row["redaction_reason"])) == (redacted, redacted)


@pytest.mark.asyncio
async def test_update_by_id_tags_only_rewrite_keeps_value_and_mirrors_facets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory evolution needs a tags/concepts rewrite that never
    touches the value: value None keeps the stored one, both rows carry the
    new facets, and the vector is recomputed from the (facet-aware) text."""
    ch = _StubCH()
    embedder = _RecordingEmbedder()
    _patch_update_by_id_deps(monkeypatch, ch=ch, embedder=embedder, blacklist_calls=[])
    out = await update_mod.handle_memory_update_by_id(
        MemoryUpdateByIdInput(
            org="test",
            memory_id="mid-123",
            tags=["Pets", "naming", " "],
            concepts=["pet_care"],
        )
    )
    assert out.updated is True
    mem_rows = ch.rows_for("loom.memory")
    emb_rows = ch.rows_for("loom.memory_embeddings")
    assert len(mem_rows) == 1 and len(emb_rows) == 1
    assert mem_rows[0]["tags"] == ["pets", "naming"] and emb_rows[0]["tags"] == ["pets", "naming"]
    assert mem_rows[0]["concepts"] == ["pet_care"] and emb_rows[0]["concepts"] == ["pet_care"]
    # the stored value survives a facets-only rewrite
    assert "FORBIDDEN" not in mem_rows[0]["value"] and mem_rows[0]["value"]
    assert any("user dog name" == c for c in embedder.calls)
