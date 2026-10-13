"""memory.reindex writes embedding rows that mirror their memory rows.

The reindexed row replaces the key's embedding row under FINAL, so every
column it carries is what search, timeline and ask then read: a default
status would serve a superseded fact as active, a default `verified` would
drop its attestation, a default `invalid_at` would un-retire it."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.memory import store as mr


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.sql.append(sql)
        self.params.append(params or {})
        if "max(version) AS ev" in sql:
            return []  # no embedding row of the key yet
        return self.rows

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserted.append((table, rows))


class _Embedder:
    model_name = "fake-model"
    dim = 3

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


def _memory_mirror_row() -> dict[str, Any]:
    t = datetime(2026, 1, 2, 3, 4, 5)
    return {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": "k",
        "memory_type": "preference",
        "content_excerpt": "x" * 600,
        "run_id": "r",
        "entities": ["e"],
        "tags": ["t"],
        "files": ["a.py"],
        "concepts": ["C"],
        "valid_at": t,
        "valid_to": None,
        "expires_at": None,
        "temporal_anchor": t,
        "temporal_confidence": 0.5,
        "reinforcement_count": 4,
        "last_reinforced_at": t,
        "mentioned_at": t,
        "source_resource_id": "res",
        "status": "superseded",
        "confidence": 0.4,
        "source_class": "extracted",
        "invalid_at": t,
        "verified": 1,
        "created_at": t,
        "version": 5,
    }


def _run(monkeypatch: Any, ch: _CH, **inp: Any) -> Any:
    return _run_with(monkeypatch, ch, _Embedder(), **inp)


def _run_with(monkeypatch: Any, ch: _CH, embedder: Any, **inp: Any) -> Any:
    import server.core.ch
    import server.embedding
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.app.ops import handle_memory_reindex

    monkeypatch.setattr(server.core.ch, "get_client", lambda: ch)
    monkeypatch.setattr(server.embedding, "get_embedder", lambda: embedder)

    async def _heads_unchanged(*_a: Any, **_k: Any) -> None:
        return None  # the post-insert head check has its own test

    monkeypatch.setattr(
        "server.memory.app.ops._retire_reindexed_rows_no_longer_served", _heads_unchanged
    )
    return asyncio.run(handle_memory_reindex(MemoryReindexInput(org="o", namespace="n", **inp)))


def test_a_reindexed_row_carries_its_memory_rows_columns(monkeypatch: Any) -> None:
    src = _memory_mirror_row()
    ch = _CH([dict(src)])
    out = _run(monkeypatch, ch)
    assert out.processed == 1
    ((table, (row,)),) = ch.inserted
    assert table == "loom.memory_embeddings"
    assert set(row) == set(mr.EMBEDDING_PERSISTED_COLS)
    for col in src.keys() - {"content_excerpt", "version"}:
        assert row[col] == src[col], col
    # one above its memory row's version (no embedding row of the key yet)
    assert row["version"] == src["version"] + 1
    # memory.set's own texts: its excerpt, and a key-axis vector beside the value's
    from server.memory.domain.embed_text import embedding_texts

    excerpt, _embed_text, key_text = embedding_texts("preference", "x" * 600, "x" * 600, "k")
    assert row["content_excerpt"] == excerpt
    assert row["embedding"] == [0.1, 0.2, 0.3] and row["embedding_dim"] == 3
    if key_text:
        assert row["memory_key_embedding"] == [0.1, 0.2, 0.3]
        assert row["memory_key_embedding_dim"] == 3
    assert row["embedding_model"] == "fake-model"


def test_the_read_skips_high_sensitivity_rows_and_current_keys_unless_forced(
    monkeypatch: Any,
) -> None:
    ch = _CH([])
    _run(monkeypatch, ch)
    _run(monkeypatch, ch, force=True)
    plain, forced = ch.sql
    for sql in (plain, forced):
        assert "m.sensitivity != 'high'" in sql
        assert "m.memory_type NOT IN {unsearchable:Array(String)}" in sql
        assert "LIMIT {lim:UInt32}" in sql
    assert "m.memory_key NOT IN" in plain and "embedding_model = {model:String}" in plain
    assert "m.memory_key NOT IN" not in forced
    assert ch.params[0]["model"] == "fake-model" and ch.params[0]["lim"] == 200
    assert ch.params[0]["unsearchable"] == ["episodic", "profile"]
    assert ch.inserted == []


class _ShortEmbedder(_Embedder):
    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3]]  # one vector short


def test_a_short_embed_batch_fails_instead_of_pairing_the_wrong_vector(
    monkeypatch: Any,
) -> None:
    two = [dict(_memory_mirror_row(), memory_key=k) for k in ("k1", "k2")]
    ch = _CH(two)
    with pytest.raises(ValueError):
        _run_with(monkeypatch, ch, _ShortEmbedder())
    assert ch.inserted == []


class _RecordingEmbedder(_Embedder):
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[float(i), 0.0, 0.0] for i, _t in enumerate(texts)]


def test_reindex_embeds_memory_sets_texts_and_pairs_the_key_vector(monkeypatch: Any) -> None:
    from server.memory.domain.embed_text import embedding_texts

    rows = [dict(_memory_mirror_row(), memory_key=k, content_excerpt=f"v {k}") for k in "ab"]
    ch = _CH(rows)
    emb = _RecordingEmbedder()
    _run_with(monkeypatch, ch, emb)
    ((_t, written),) = ch.inserted
    want = [embedding_texts("preference", f"v {k}", f"v {k}", k) for k in "ab"]
    keyed = [w[2] for w in want if w[2]]
    assert emb.texts == [w[1] for w in want] + keyed
    for i, (row, (excerpt, _e, key)) in enumerate(zip(written, want, strict=True)):
        assert row["content_excerpt"] == excerpt
        assert row["embedding"] == [float(i), 0.0, 0.0]
        if key:
            assert row["memory_key_embedding"] == [float(2 + keyed.index(key)), 0.0, 0.0]


def test_a_procedural_row_is_reindexed_from_its_procedure_text() -> None:
    import json

    from server.memory.app.ops import _reindex_texts
    from server.memory.domain.embed_text import embedding_texts

    proc = {"situation": "deploy fails on a cold cache", "name": "warm_cache", "steps": ["a"]}
    got = _reindex_texts(
        {"memory_type": "procedural", "memory_key": "p", "content_excerpt": json.dumps(proc)}
    )
    assert got == embedding_texts("procedural", proc, json.dumps(proc), "p")
    assert "cold cache" in got[1] and "{" not in got[1]


def test_a_key_made_high_during_the_reindex_has_its_vectors_retired(monkeypatch: Any) -> None:
    """The heads are re-read after the insert: a key whose head is now 'high' (or
    gone) is retired at a version drawn then, above the reindexed row."""
    from types import SimpleNamespace

    from server.memory.app import ops

    retired: list[tuple[Any, int]] = []

    async def _heads(keys: Any, **_k: Any) -> dict[Any, Any]:
        return {
            keys[0]: SimpleNamespace(memory_type="semantic", sensitivity="high"),
            keys[1]: SimpleNamespace(memory_type="semantic", sensitivity=""),
        }  # keys[2] is gone

    async def _retire(items: Any, **_k: Any) -> int:
        retired.extend(items)
        return len(items)

    monkeypatch.setattr(mr, "memory_rows_by_identity_keys", _heads)
    monkeypatch.setattr(mr, "retire_vectors_for_keys", _retire)
    rows = [{**_memory_mirror_row(), "memory_key": k, "version": 6} for k in ("k1", "k2", "k3")]
    asyncio.run(ops._retire_reindexed_rows_no_longer_served(rows, ch=object()))
    assert [k[5] for k, _ in retired] == ["k1", "k3"]
    assert all(v > 6 for _, v in retired)
