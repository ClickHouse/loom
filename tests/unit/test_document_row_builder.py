"""loom.documents is ReplacingMergeTree(version): a version row that leaves a
column out resets it for every FINAL reader. Every document write goes through
`documents_repo.build_document_row`, which these tests pin to schema.sql and
check on the write paths."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.knowledge.app import writes as kh
from server.knowledge.app.writes import _content_sha256
from server.knowledge.store import chunks as knowledge_repo
from server.knowledge.store import documents as documents_repo
from server.knowledge.store.documents import DocumentRow
from tests.unit.schema_ddl import column_names

ERASED = datetime(2026, 5, 1, 12, 0, 0)
INGESTED = datetime(2026, 4, 1, 9, 0, 0)


def _doc(**over: Any) -> DocumentRow:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "source_uri": "doc-a",
        "doc_id": "11111111-1111-1111-1111-111111111111",
        "title": "Old title",
        "description": "desc",
        "content": "body text",
        "tags": ["t"],
        "metadata": '{"k": 1}',
        "version": 1,
        "ingested_at": INGESTED,
        "content_sha256": _content_sha256("Old title\nbody text"),
        "blob_path": "o/n/blob",
        "blob_size": 42,
        "blob_content_type": "application/pdf",
        "original_filename": "a.pdf",
        "blob_erased_at": ERASED,
    }
    base.update(over)
    return DocumentRow(**base)


def test_builder_keys_are_the_schema_columns_a_writer_carries() -> None:
    row = documents_repo.build_document_row(
        org="o", namespace="n", source_uri="u", content="c", version=1
    )
    assert list(row) == column_names("documents", materialized=False)
    assert list(documents_repo.DOCUMENT_COLUMNS) == column_names("documents", materialized=False)


def test_document_row_reads_every_carried_column() -> None:
    assert set(documents_repo.DOCUMENT_COLUMNS) <= set(DocumentRow.model_fields)


def test_builder_carries_an_existing_row_and_recomputes_the_hash() -> None:
    doc = _doc()
    row = documents_repo.build_document_row(doc, version=2, superseded_at=ERASED)
    for col in documents_repo.DOCUMENT_COLUMNS:
        if col not in ("version", "superseded_at"):
            assert row[col] == getattr(doc, col), col
    renamed = documents_repo.build_document_row(doc, title="New", version=3)
    assert renamed["content_sha256"] == _content_sha256("New\nbody text")


def test_builder_refuses_unknown_and_hash_fields() -> None:
    with pytest.raises(ValueError):
        documents_repo.build_document_row(_doc(), version=2, bogus=1)
    with pytest.raises(ValueError):
        documents_repo.build_document_row(_doc(), version=2, content_sha256="x")


def test_the_repo_reads_select_the_hash_and_the_erasure_stamp() -> None:
    seen: list[str] = []

    class _CH:
        async def query_one(self, sql, params=None):
            seen.append(sql)
            return None

        async def query_all(self, sql, params=None):
            seen.append(sql)
            return []

    asyncio.run(documents_repo.get_one("o", "n", "u", ch=_CH()))
    asyncio.run(documents_repo.get_many_by_uris("o", "n", ["u"], ch=_CH()))
    assert len(seen) == 2, seen
    for sql in seen:
        assert "content_sha256" in sql and "blob_erased_at" in sql


def _capture_inserts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def _one(row, *, ch=None):
        rows.append(row)

    async def _many(batch, *, ch=None):
        rows.extend(batch)
        return len(batch)

    monkeypatch.setattr(documents_repo, "insert_one", _one)
    monkeypatch.setattr(documents_repo, "insert_many", _many)
    return rows


def test_update_metadata_keeps_the_hash_and_the_erasure_stamp(monkeypatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput

    rows = _capture_inserts(monkeypatch)

    async def _get_one(*a, **kw):
        return _doc()

    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    asyncio.run(
        kh.handle_knowledge_update_metadata(
            KnowledgeUpdateMetadataInput(org="o", namespace="n", source_uri="doc-a", title="New")
        )
    )
    [row] = rows
    assert row["content_sha256"] == _content_sha256("New\nbody text")
    assert row["blob_erased_at"] == ERASED
    assert row["ingested_at"] == INGESTED
    assert row["blob_path"] == "o/n/blob"


def test_delete_tombstones_carry_every_column(monkeypatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput, KnowledgeDeleteInput

    rows = _capture_inserts(monkeypatch)
    doc = _doc()

    async def _count(*a, **kw):
        return 1

    async def _counts(*a, **kw):
        return {"doc-a": 1}

    async def _noop(*a, **kw):
        return None

    async def _get_one(*a, **kw):
        return doc

    async def _get_many(*a, **kw):
        return [doc]

    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", _noop)
    monkeypatch.setattr(knowledge_repo, "mark_docs_superseded", _noop)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    from server.knowledge.store import datasets as datasets_repo

    retired: list[dict[str, int]] = []

    async def _forget(org, ns, through, **kw):
        retired.append(dict(through))

    monkeypatch.setattr(datasets_repo, "forget_published", _forget)

    asyncio.run(
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
    )
    asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a"])
        )
    )
    assert len(rows) == 2
    assert retired == [{"doc-a": doc.version}] * 2  # each delete's datasets, at the read version
    for row in rows:
        assert row["superseded_at"] is not None
        for col in ("description", "content_sha256", "blob_path", "blob_size", "blob_erased_at"):
            assert row[col] == getattr(doc, col), col


class _FakeEmbedder:
    model_name = "fake"
    dim = 4

    async def embed(self, text: str) -> list[float]:
        return [0.0] * self.dim

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


def test_bulk_add_sets_the_hash_and_keeps_a_prior_blob(monkeypatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeBulkAddInput

    rows = _capture_inserts(monkeypatch)

    async def _chunks(batch, *, ch=None):
        return len(batch)

    async def _get_many(*a, **kw):
        # a live prior: its download carries (an erased or deleted one's would not)
        return [_doc(source_uri="doc-a", blob_erased_at=None)]

    monkeypatch.setattr(knowledge_repo, "insert_chunks", _chunks)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(kh, "get_embedder", lambda: _FakeEmbedder())
    asyncio.run(
        kh.handle_knowledge_bulk_add(
            KnowledgeBulkAddInput(
                org="o",
                namespace="n",
                entries=[
                    {"source_uri": "doc-a", "title": "A", "content": "alpha"},
                    {"source_uri": "doc-b", "title": "B", "content": "beta"},
                ],
            )
        )
    )
    by_uri = {r["source_uri"]: r for r in rows}
    assert by_uri["doc-a"]["content_sha256"] == _content_sha256("A\nalpha")
    assert by_uri["doc-b"]["content_sha256"] == _content_sha256("B\nbeta")
    assert by_uri["doc-a"]["blob_path"] == "o/n/blob"
    assert by_uri["doc-a"]["original_filename"] == "a.pdf"
    assert by_uri["doc-b"]["blob_path"] == ""
