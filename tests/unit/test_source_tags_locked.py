"""A document the sync worker wrote keeps the `source:*` tags the worker gave it:
another writer that keeps the worker as the author (a tag edit, a re-save of the
same text) cannot add or drop one. A caller's own documents take any tags."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core.errors import ValidationError
from server.knowledge.api.models_knowledge import (
    KnowledgeAddInput,
    KnowledgeBulkAddInput,
    KnowledgeUpdateMetadataInput,
    KnowledgeUpsertInput,
)
from server.knowledge.app import writes as kh
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.knowledge.store import chunks as knowledge_repo
from server.knowledge.store import documents as documents_repo
from server.memory.domain.provenance import pipeline_actor
from tests.unit.test_document_row_builder import _capture_inserts, _doc, _FakeEmbedder

# The single-document writes, each with its input model.
RESAVES = pytest.mark.parametrize(
    ("model", "handler"),
    [
        (KnowledgeAddInput, kh.handle_knowledge_add),
        (KnowledgeUpsertInput, kh.handle_knowledge_upsert),
    ],
    ids=["add", "upsert"],
)

WORKER_DOC = {"actor": SYNC_WORKER_ACTOR, "tags": ["source:decks", "team:db"], "blob_path": ""}


def _prior(monkeypatch: pytest.MonkeyPatch, **over: Any) -> None:
    async def _get_one(*a: Any, **kw: Any) -> Any:
        return _doc(**over)

    async def _get_many(*a: Any, **kw: Any) -> Any:
        return [_doc(**over)]

    async def _chunks(batch: Any, *, ch: Any = None) -> int:
        return len(batch)

    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(knowledge_repo, "insert_chunks", _chunks)
    monkeypatch.setattr(kh, "get_embedder", lambda: _FakeEmbedder())


def _retag(tags: list[str]) -> Any:
    body = KnowledgeUpdateMetadataInput(org="o", namespace="n", source_uri="doc-a", tags=tags)
    return kh.handle_knowledge_update_metadata(body)


def _resave(tags: list[str], content: str = "body text") -> Any:
    entry = {"source_uri": "doc-a", "title": "Old title", "content": content, "tags": tags}
    return kh.handle_knowledge_bulk_add(
        KnowledgeBulkAddInput(org="o", namespace="n", entries=[entry])
    )


@pytest.mark.parametrize("tags", [["team:db"], ["source:decks", "source:other", "team:db"]])
def test_a_tag_edit_of_a_worker_document_cannot_change_its_source_tags(
    monkeypatch: pytest.MonkeyPatch, tags: list[str]
) -> None:
    rows = _capture_inserts(monkeypatch)
    _prior(monkeypatch, **WORKER_DOC)
    with pytest.raises(ValidationError) as e:
        asyncio.run(_retag(tags))
    assert e.value.to_detail()["code"] == "SOURCE_TAGS_LOCKED"
    assert rows == []


def test_a_tag_edit_of_a_worker_document_may_change_its_other_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _capture_inserts(monkeypatch)
    _prior(monkeypatch, **WORKER_DOC)
    asyncio.run(_retag(["source:decks", "team:storage"]))
    assert rows[0]["tags"] == ["source:decks", "team:storage"]


def test_a_callers_own_document_takes_any_source_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _capture_inserts(monkeypatch)
    _prior(monkeypatch, actor="human:alice", tags=["t"])
    asyncio.run(_retag(["source:decks"]))
    assert rows[0]["tags"] == ["source:decks"]


def test_a_resave_keeping_the_worker_as_author_cannot_change_source_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _capture_inserts(monkeypatch)
    _prior(monkeypatch, **WORKER_DOC)
    for tags in (["team:db"], ["source:decks", "source:other"]):
        with pytest.raises(ValidationError, match="source:\\* tags belong to the sync"):
            asyncio.run(_resave(tags))
    assert rows == []
    # The same source tags pass, and new text makes the writer the author.
    asyncio.run(_resave(["source:decks"]))
    asyncio.run(_resave(["team:db"], content="new text"))
    assert [r["actor"] for r in rows] == [SYNC_WORKER_ACTOR, ""]


def test_the_sync_worker_itself_may_change_its_source_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _capture_inserts(monkeypatch)
    _prior(monkeypatch, **WORKER_DOC)
    with pipeline_actor(SYNC_WORKER_ACTOR):
        asyncio.run(_retag(["source:renamed"]))
    assert rows[0]["tags"] == ["source:renamed"]


def _stub_upsert_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _one_live_chunk(*a: Any, **kw: Any) -> int:
        return 1  # the document is live, so upsert reads it as the prior

    async def _no_duplicate(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _one_live_chunk)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", _no_duplicate)


@RESAVES
def test_an_add_or_upsert_resave_cannot_change_source_tags(
    monkeypatch: pytest.MonkeyPatch, model: Any, handler: Any
) -> None:
    rows = _capture_inserts(monkeypatch)
    _prior(monkeypatch, **WORKER_DOC)
    _stub_upsert_reads(monkeypatch)
    for tags in (["team:db"], ["source:decks", "source:other"]):
        body = model(
            org="o",
            namespace="n",
            source_uri="doc-a",
            title="Old title",
            content="body text",
            tags=tags,
        )
        with pytest.raises(ValidationError) as e:
            asyncio.run(handler(body))
        assert e.value.to_detail()["code"] == "SOURCE_TAGS_LOCKED"
    assert rows == []


def test_a_bulk_add_with_one_locked_entry_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _capture_inserts(monkeypatch)
    _prior(monkeypatch, **WORKER_DOC)
    embedded: list[str] = []

    class _Recording(_FakeEmbedder):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            embedded.extend(texts)
            return await super().embed_many(texts)

    monkeypatch.setattr(kh, "get_embedder", lambda: _Recording())
    entries = [
        {"source_uri": "doc-b", "title": "B", "content": "beta", "tags": []},
        {"source_uri": "doc-a", "title": "Old title", "content": "body text", "tags": []},
    ]
    with pytest.raises(ValidationError):
        asyncio.run(
            kh.handle_knowledge_bulk_add(
                KnowledgeBulkAddInput(org="o", namespace="n", entries=entries)
            )
        )
    assert rows == [] and embedded == []


@RESAVES
def test_an_add_or_upsert_resave_with_the_same_source_tags_passes(
    monkeypatch: pytest.MonkeyPatch, model: Any, handler: Any
) -> None:
    rows = _capture_inserts(monkeypatch)
    _prior(monkeypatch, **WORKER_DOC)
    _stub_upsert_reads(monkeypatch)

    async def _one(*a: Any, **kw: Any) -> int:
        return 1

    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", _one)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", _one)
    body = model(
        org="o",
        namespace="n",
        source_uri="doc-a",
        title="Old title",
        content="body text",
        tags=["source:decks", "team:storage"],
    )
    asyncio.run(handler(body))
    [row] = [r for r in rows if r["source_uri"] == "doc-a"]
    assert row["tags"] == ["source:decks", "team:storage"]
    assert row["actor"] == SYNC_WORKER_ACTOR  # the same text keeps the worker as author
