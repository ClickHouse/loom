"""Every path that writes or re-writes a vector keeps a 'high' key out of search:
consolidation (candidates and the merged row), reindex, restore, and the fence a
key made high leaves on its verbatim chunks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_admin import MemoryConsolidateInput


def test_consolidation_candidates_are_live_vectors_of_plain_rows() -> None:
    from server.memory.app.consolidate import _CONSOLIDATION_CANDIDATES_SQL as sql

    assert "e.tombstoned_at IS NULL" in sql and "e.embedding_dim > 0" in sql
    assert "m.sensitivity != 'high'" in sql
    assert "m.redacted_at IS NULL" in sql  # a redacted row's text never reaches the model


@pytest.mark.asyncio
async def test_a_merged_row_that_reads_as_high_gets_no_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.store as memory_repo
    from server.memory.app import consolidate, high_fence

    inserted: list[str] = []
    fenced: list[tuple[Any, int]] = []

    class _CH:
        async def insert(self, table: str, rows: Any, **_: Any) -> None:
            inserted.append(table)

    class _Embedder:
        model_name, dim = "fake", 3

        async def embed(self, text: str) -> list[float]:
            raise AssertionError("a high merged row is never embedded")

    async def _mem(rows: Any, **_: Any) -> None:
        inserted.append("loom.memory")

    async def _fence(items: Any, **_: Any) -> None:
        fenced.extend(items)

    async def _links(**_: Any) -> None:
        return None

    monkeypatch.setattr(memory_repo, "insert_memory_versions", _mem)
    monkeypatch.setattr(high_fence, "fence_high", _fence)
    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _links)
    cluster = [
        {
            "memory_id": "m-a",
            "version": 1,
            "memory_key": "a",
            "memory_type": "semantic",
            "sensitivity": "",
            "tags": [],
        },
        {
            "memory_id": "m-b",
            "version": 1,
            "memory_key": "b",
            "memory_type": "semantic",
            "sensitivity": "",
            "tags": [],
        },
    ]

    async def _no_head(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.store.get_by_key", _no_head)
    row = await consolidate._write_canonical(
        _CH(),
        MemoryConsolidateInput(org="o", namespace="n", agent="a"),
        _Embedder(),
        cluster,
        canonical_key="merged",  # a key of its own: no head to write over
        canonical_value="the deploy key is sk-proj-test-ABCDEFGHIJKLMNOPQRSTUVWX0123",
        now_naive=datetime(2026, 10, 1),
        claimed=set(),
    )
    assert row["sensitivity"] == "high"
    assert inserted == ["loom.memory"]  # no memory_embeddings row
    assert [(k[5], v) for k, v in fenced] == [("merged", row["version"])]


@pytest.mark.asyncio
async def test_reindex_stamps_its_rows_above_the_keys_newest_row() -> None:
    import server.memory.store as memory_repo

    ident = {"agent": "a", "user_id": "-", "session_id": "-", "memory_key": "k"}

    class _CH:
        def __init__(self) -> None:
            self.calls = 0

        async def query_all(self, sql: str, params: Any = None) -> list[Any]:
            self.calls += 1
            if self.calls == 1:
                return [{**ident, "version": 10}, {**ident, "memory_key": "k2", "version": 20}]
            assert "max(version) AS ev" in sql and params["model"] == "m"
            return [{**ident, "ev": 15}]

    rows = await memory_repo.find_memories_to_reindex(
        "o", "n", embedding_model="m", force=False, limit=5, ch=_CH()
    )
    # above the newer embedding row of k; above its own memory row for k2
    assert [r["version"] for r in rows] == [16, 21]


@pytest.mark.asyncio
async def test_restoring_a_high_row_writes_no_embedding_row() -> None:
    from types import SimpleNamespace

    from server.memory.app import restore

    class _CH:
        async def query_all(self, *_a: Any, **_k: Any) -> list[Any]:
            raise AssertionError("no embeddings read for a high row")

        async def insert(self, *_a: Any, **_k: Any) -> None:
            raise AssertionError("no embeddings row for a high row")

    await restore._restore_embedding_mirror(
        _CH(), SimpleNamespace(), SimpleNamespace(sensitivity="high"), "semantic", version=1
    )


@pytest.mark.asyncio
async def test_fence_high_fences_vectors_and_tombstones_each_keys_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.store as memory_repo
    from server.memory.app import high_fence

    fenced: list[Any] = []
    chunks: list[tuple[str, str, str, str]] = []

    async def _fence(items: Any, *, model: str, ch: Any = None) -> int:
        fenced.extend(items)
        return len(items)

    async def _chunks(
        *, org: str, namespace: str, memory_key: str, user_id: str, ch: Any, strong: bool
    ) -> int:
        assert strong  # sequentially consistent: a chunk insert on another replica is seen
        chunks.append((org, namespace, memory_key, user_id))
        return 1

    monkeypatch.setattr(memory_repo, "fence_vectors", _fence)
    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _chunks)
    key = ("o", "n", "a", "u1", "s", "k")
    await high_fence.fence_high([(key, 7)], ch=object())
    assert fenced == [(key, 7)]
    assert chunks == [("o", "n", "k", "u1")]
