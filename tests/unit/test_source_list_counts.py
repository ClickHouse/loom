"""A checked list counts only its copy's served logical chunks, including stubs."""

from __future__ import annotations

import pytest

from server.knowledge.domain.serving import Serving
from server.knowledge.store import source_documents as store
from tests.unit.ch_fake import RecordingCH

NS = "acl-" + "1" * 24
URI = "gdrive:file#11111111-1111-4111-8111-111111111111"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(store.pg, "enabled", lambda: False)
    ch = RecordingCH().on("FROM loom.chunks", [{"chunk_count": 2}])
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    return ch


async def test_count_deduplicates_logical_sections_and_includes_embedding_stubs(world):
    assert await store.list_chunk_count("acme", NS, URI, 0, None) == 2
    statement = world.calls[-1]
    assert statement.params == {"org": "acme", "ns": NS, "uri": URI, "revision": 0}
    assert "uniqExact(embedding_model, chunk_idx)" in statement.sql
    assert "embedding_dim" not in statement.sql
    assert "doc_superseded_at IS NULL" in statement.sql and "chunk_supersessions" in statement.sql
    assert "JSONExtractUInt(chunk_metadata, 'revision') = {revision:UInt64}" in statement.sql
    assert "doc_version =" not in statement.sql


async def test_pg_counts_exact_recorded_chunk_version_not_metadata_only_row_version(
    world, monkeypatch
):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    assert await store.list_chunk_count("acme", NS, URI, 2, Serving(False, 2, 2, 10)) == 2
    statement = world.calls[-1]
    assert statement.params == {"org": "acme", "ns": NS, "uri": URI, "revision": 2, "version": 10}
    assert "doc_version = {version:UInt64}" in statement.sql


@pytest.mark.parametrize("head", [Serving(False, 2, None, None), Serving(False, 2, 1, 10)])
async def test_pg_unpublished_current_revision_has_zero_chunks_without_ch_query(
    world, monkeypatch, head
):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    assert await store.list_chunk_count("acme", NS, URI, 2, head) == 0
    assert not world.calls


@pytest.mark.parametrize("head", [None, Serving(True, 2, 2, 10), Serving(False, 3, 3, 10)])
async def test_missing_or_mismatched_pg_count_proof_is_refused(world, monkeypatch, head):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    with pytest.raises(ValueError):
        await store.list_chunk_count("acme", NS, URI, 2, head)
    assert not world.calls
