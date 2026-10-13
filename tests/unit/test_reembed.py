"""Unit: reembed row-rewrite (server.memory.app.reembed.rewrite_row)."""

from __future__ import annotations

from server.memory.app.reembed import _TABLES, rewrite_row


def test_rewrite_row_memory_embeddings_repoints_and_preserves():
    cfg = _TABLES["memory_embeddings"]
    row = {
        "org": "o",
        "memory_key": "k",
        "content_excerpt": "c",
        "embedding": [0.1] * 1536,
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1536,
        "memory_key_embedding": [0.2] * 1536,
        "memory_key_embedding_dim": 1536,
        "tags": ["x"],
        "version": 1,
    }
    out = rewrite_row(row, cfg, "text-embedding-3-large", 3072, [0.9] * 3072, [0.8] * 3072)
    assert out["embedding_model"] == "text-embedding-3-large"
    assert out["embedding_dim"] == 3072 and len(out["embedding"]) == 3072
    assert out["memory_key_embedding_dim"] == 3072
    assert len(out["memory_key_embedding"]) == 3072
    assert out["tags"] == ["x"]  # metadata preserved
    # one above the row read: it replaces that row, and loses to any version a
    # live writer stamps after the snapshot (wall-clock ns)
    assert out["version"] == 2


def test_rewrite_row_entity_vectors_drops_created_at():
    cfg = _TABLES["entity_vectors"]
    row = {
        "org": "o",
        "entity_text": "e",
        "entity_embedding": [0.1] * 1536,
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1536,
        "created_at": "2020-01-01 00:00:00",
    }
    out = rewrite_row(row, cfg, "text-embedding-3-large", 3072, [0.9] * 3072, None)
    # created_at dropped so DEFAULT now() wins the ReplacingMergeTree merge.
    assert "created_at" not in out
    assert out["embedding_dim"] == 3072 and len(out["entity_embedding"]) == 3072
    assert "memory_key_embedding" not in out  # no key vector for this table


def test_a_rewrite_never_outranks_a_write_after_the_snapshot():
    """The run reads a snapshot, then inserts batches later. A live write or
    delete of the same key in between carries a wall-clock version; the
    rewrite of the older snapshot row must lose to it under FINAL."""
    import time

    cfg = _TABLES["memory_embeddings"]
    snapshot_version = time.time_ns()
    live_write_version = time.time_ns() + 1_000
    row = {"memory_key": "k", "embedding_model": "old", "version": snapshot_version}
    out = rewrite_row(row, cfg, "new", 4, [0.1] * 4, None)
    assert snapshot_version < out["version"] < live_write_version


def test_a_width_the_coded_tables_do_not_take_is_refused_before_any_read(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import pytest

    from server.core.errors import CodedWidthMismatchError
    from server.core.vectors import codes_state
    from server.memory.app.reembed import reembed_all

    class _NoQueries:
        def __getattr__(self, name):
            raise AssertionError(f"reembed read ClickHouse ({name}) before refusing")

    monkeypatch.setattr(
        codes_state,
        "_widths",
        {"loom.memory_embeddings.embedding": 3072, "loom.code_chunks.embedding": 512},
    )
    embedder = SimpleNamespace(model_name="fake-ngram-512", dim=512)
    with pytest.raises(CodedWidthMismatchError, match="stores 3072-dim vectors"):
        asyncio.run(reembed_all(_NoQueries(), embedder))
