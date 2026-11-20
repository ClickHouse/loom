"""Synthesis alone assembles checked chunks; ordinary and mining helpers stay scoped."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app import source_corpus as app
from server.knowledge.app import source_documents
from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_search import Hydrated
from server.knowledge.domain.serving import Serving
from server.knowledge.store import documents
from server.memory.domain.corpus_synthesis import CorpusChunk

NS = "acl-" + "1" * 24
URI = "gdrive:file#native"


@pytest.fixture
def world(monkeypatch):
    metadata = {
        "tags": [],
        "version": 11,
        "revision": 0,
        "ingested_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    candidate = {"namespace": NS, "source_uri": URI + "#source"}
    admitted = Admitted(
        "team", candidate, {**candidate, **metadata}, serving=Serving(False, 0, 0, 10)
    )
    checked = source_documents.CheckedDocument(
        admitted,
        SimpleNamespace(
            org="acme",
            **candidate,
            **metadata,
            content="Full body not retained as proof",
        ),
    )
    hydrated = Hydrated(
        admitted,
        {
            **candidate,
            "chunk_idx": 3,
            "content": "Checked chunk",
            "doc_version": 10,
            "chunk_metadata": '{"revision":0,"section_path":["Notes"],"locator":{"page":2}}',
        },
    )
    plan = SimpleNamespace(
        org="acme",
        failed=False,
        unread=False,
        current=lambda _item: True,
        check=AsyncMock(return_value=[admitted]),
    )
    ordinary = AsyncMock(return_value=[])
    monkeypatch.setattr(documents, "get_one", AsyncMock(return_value=None))
    monkeypatch.setattr(source_documents, "get", AsyncMock(return_value=checked))
    monkeypatch.setattr(source_documents, "chunks", AsyncMock(return_value=[hydrated]))
    return plan, ordinary, checked, hydrated


async def load(world, uris=None):
    plan, ordinary, _checked, _hydrated = world
    return await app.load(plan, "team", uris or [URI], ordinary_chunks=ordinary, model="fixture")


async def test_admitted_chunks_keep_public_uri_structure_and_recorded_serving_pair(world):
    result = await load(world)
    assert result.documents == 1 and result.proofs.mirrored
    assert result.chunks == [CorpusChunk(URI, 3, "Checked chunk", ("Notes",), 0, {"page": 2})]
    source_documents.chunks.assert_awaited_once_with(world[0], world[2], model="fixture")


async def test_existing_ordinary_chunks_win_even_without_document_head(world):
    _plan, ordinary, _checked, _hydrated = world
    ordinary.return_value = [CorpusChunk(URI, 1, "Ordinary legacy chunk")]
    result = await load(world)
    assert result.chunks == ordinary.return_value and result.documents == 1
    assert not result.proofs.mirrored
    documents.get_one.assert_not_awaited()
    source_documents.get.assert_not_awaited()


async def test_live_ordinary_document_with_zero_usable_chunks_still_shadows_mirror(world):
    documents.get_one.return_value = object()
    result = await load(world)
    assert result.chunks == [] and result.documents == 0 and not result.proofs.mirrored
    source_documents.get.assert_not_awaited()


async def test_ordinary_presence_failure_is_not_mirror_fallback(world):
    documents.get_one.side_effect = RuntimeError("ordinary authority unavailable")
    with pytest.raises(RuntimeError, match="ordinary authority"):
        await load(world)
    source_documents.get.assert_not_awaited()


async def test_denied_copy_contributes_no_chunks_document_or_proof(world):
    source_documents.get.return_value = None
    result = await load(world)
    assert result.chunks == [] and result.documents == 0 and not result.proofs.mirrored
    source_documents.chunks.assert_not_awaited()


@pytest.mark.parametrize("chunks", [None, []])
async def test_empty_or_failed_checked_chunk_read_contributes_nothing(world, chunks):
    source_documents.chunks.return_value = chunks
    result = await load(world)
    assert result.chunks == [] and result.documents == 0 and not result.proofs.mirrored


async def test_later_mirror_failure_discards_every_mirror_but_preserves_ordinary_corpus(world):
    plan, ordinary, _checked, _hydrated = world
    ordinary.side_effect = [[], [], [CorpusChunk("ordinary", 0, "Ordinary")]]

    async def get(_plan, _namespace, uri):
        if uri != URI:
            plan.failed = True
            return None
        return world[2]

    source_documents.get.side_effect = get
    result = await load(world, [URI, "other", "ordinary"])
    assert result.chunks == [CorpusChunk("ordinary", 0, "Ordinary")]
    assert result.documents == 1 and not result.proofs.mirrored and not plan.unread
    assert source_documents.get.await_count == 2


async def test_final_permission_loss_discards_mirror_corpus_before_any_model_use(world):
    plan, _ordinary, _checked, _hydrated = world
    plan.check.return_value = []
    result = await load(world)
    assert result.chunks == [] and result.documents == 0 and not result.proofs.mirrored
    assert not plan.unread


@pytest.mark.parametrize("later_chunks", [None, []])
async def test_later_chunk_failure_discards_earlier_mirrors_but_empty_copy_does_not(
    world, later_chunks
):
    source_documents.chunks.side_effect = [[world[3]], later_chunks]
    result = await load(world, [URI, "other"])
    assert result.documents == (0 if later_chunks is None else 1)
    assert result.proofs.mirrored == (later_chunks is not None)
    assert world[0].failed == (later_chunks is None)


async def test_cancellation_propagates_without_returning_a_partial_corpus(world):
    import asyncio

    source_documents.chunks.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await load(world)
    assert not world[0].failed


async def test_repeated_public_uri_reuses_one_whole_copy_if_another_source_changes(world):
    from dataclasses import replace

    plan, _ordinary, checked, hydrated = world
    newer = replace(
        checked.admitted,
        candidate={**checked.admitted.candidate, "source_uri": "another-copy"},
    )
    other = source_documents.CheckedDocument(
        newer, SimpleNamespace(**{**vars(checked.row), "source_uri": "another-copy"})
    )
    source_documents.get.side_effect = [checked, other]
    source_documents.chunks.side_effect = [
        [hydrated],
        [replace(hydrated, admitted=newer, row={**hydrated.row, "content": "Another copy"})],
    ]
    by_uri = {item.candidate["source_uri"]: item for item in (checked.admitted, newer)}

    async def current(candidates):
        return [by_uri[row["source_uri"]] for row in candidates]

    plan.check.side_effect = current
    result = await load(world, [URI, URI])
    assert [chunk.text for chunk in result.chunks] == ["Checked chunk", "Checked chunk"]
    assert result.documents == 2
    source_documents.get.assert_awaited_once()


@pytest.mark.parametrize("field", ["namespace", "source_uri", "version", "revision", "serving"])
async def test_chunks_must_belong_to_the_same_complete_admitted_copy(world, field):
    from dataclasses import replace

    _plan, _ordinary, _checked, hydrated = world
    item = hydrated.admitted
    if field in {"namespace", "source_uri"}:
        item = replace(item, candidate={**item.candidate, field: "other"})
    elif field == "serving":
        item = replace(item, serving=Serving(False, 0, 0, 9))
    else:
        item = replace(item, document={**item.document, field: item.document[field] + 1})
    source_documents.chunks.return_value = [replace(hydrated, admitted=item)]
    result = await load(world)
    assert result.chunks == [] and result.documents == 0 and not result.proofs.mirrored
