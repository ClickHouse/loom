"""knowledge.add, knowledge.upsert and knowledge.bulk_add write the same
loom.chunks row: every column, the chunk's context and content hash included (upsert's
duplicate count and the dedup report match on it), and naive-UTC times.
CH-free."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.core.hashing import sha256_document_text
from server.knowledge.api.models_knowledge import (
    KnowledgeAddInput,
    KnowledgeBulkAddInput,
    KnowledgeUpsertInput,
)
from server.knowledge.app import writes as kh
from server.knowledge.store import chunks as chunks_repo
from server.knowledge.store import documents as documents_repo

DOC = "# Deploy\n\nRoll forward first.\n\n## Rollback\n\nRevert the image tag."


class _Embedder:
    model_name = "fake-e"
    dim = 3

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 0.0, 1.0] for t in texts]


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def _none(*a: Any, **kw: Any) -> None:
        return None

    async def _zero(*a: Any, **kw: Any) -> int:
        return 0

    async def _empty(*a: Any, **kw: Any) -> list[Any]:
        return []

    async def _chunks(batch: list[dict[str, Any]], **kw: Any) -> int:
        rows.extend(batch)
        return len(batch)

    monkeypatch.setattr(kh, "get_embedder", _Embedder)
    monkeypatch.setattr(documents_repo, "get_one", _none)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _empty)
    monkeypatch.setattr(documents_repo, "insert_one", _none)
    monkeypatch.setattr(documents_repo, "insert_many", _none)
    monkeypatch.setattr(chunks_repo, "find_doc_by_content_sha256", _none)
    monkeypatch.setattr(chunks_repo, "count_active_for_doc", _zero)
    monkeypatch.setattr(chunks_repo, "count_matching_chunk_hashes", _zero)
    monkeypatch.setattr(chunks_repo, "insert_chunks", _chunks)
    return rows


async def _add() -> None:
    await kh.handle_knowledge_add(
        KnowledgeAddInput(org="o", namespace="n", source_uri="doc://a", content=DOC, title="T")
    )


async def _upsert() -> None:
    await kh.handle_knowledge_upsert(
        KnowledgeUpsertInput(org="o", namespace="n", source_uri="doc://a", content=DOC, title="T")
    )


async def _bulk_add() -> None:
    await kh.handle_knowledge_bulk_add(
        KnowledgeBulkAddInput(
            org="o",
            namespace="n",
            entries=[{"source_uri": "doc://a", "content": DOC, "title": "T"}],
        )
    )


@pytest.mark.parametrize("write", [_add, _upsert, _bulk_add])
async def test_every_writer_sets_every_chunk_column(written: list[dict[str, Any]], write) -> None:
    await write()
    assert len(written) >= 2, written  # two sections, two chunks at least
    assert set(written[0]) == set(chunks_repo.ChunkInsertRow.__annotations__)
    for r in written:
        assert r["content_sha256"] == sha256_document_text(r["content"])
        assert r["embedding_dim"] == 3 and len(r["embedding"]) == 3
        assert isinstance(r["ingested_at"], datetime) and r["ingested_at"].tzinfo is None
        assert r["created_at"] == r["ingested_at"]
        # the context is what the vector embeds before the text, and is stored with it
        assert r["context"].startswith("T\nDeploy")
        assert r["embedding"][0] == float(len(r["context"] + "\n\n" + r["content"]))
    assert [r["chunk_idx"] for r in written] == list(range(len(written)))
    assert [r["context"] for r in written[:2]] == ["T\nDeploy", "T\nDeploy > Rollback"]


async def test_the_three_writers_write_the_same_rows(written: list[dict[str, Any]]) -> None:
    def _stable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        vary = {"ingested_at", "created_at", "doc_version"}
        return [{k: v for k, v in r.items() if k not in vary} for r in rows]

    out = []
    for write in (_add, _upsert, _bulk_add):
        written.clear()
        await write()
        out.append(_stable(written))
    assert out[0] == out[1] == out[2]
