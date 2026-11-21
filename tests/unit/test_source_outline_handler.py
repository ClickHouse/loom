"""Outlines project one checked copy and preserve ordinary mining reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeOutlineInput
from server.knowledge.app import document_reads as app
from server.knowledge.app import source_access, source_documents
from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_search import Hydrated
from server.knowledge.store import chunks as chunk_store
from server.knowledge.store import documents


@pytest.fixture
def world(monkeypatch):
    phases = []
    plan = SimpleNamespace(marked=True)
    input = KnowledgeOutlineInput(
        org="acme", namespace="team", source_uri="gdrive:file#native", section_path=["Pending"]
    )
    metadata = {
        "namespace": "acl-" + "a" * 24,
        "source_uri": input.source_uri,
        "version": 1,
        "revision": 0,
        "ingested_at": datetime.now(UTC),
    }
    checked = source_documents.CheckedDocument(
        Admitted("team", dict(metadata), dict(metadata)), SimpleNamespace(title="Checked title")
    )
    chunk = Hydrated(
        Admitted("team", {**metadata, "chunk_idx": 1, "doc_version": 1}, dict(metadata)),
        {
            "content": "Unembedded section.",
            "chunk_metadata": json.dumps(
                {
                    "section_path": ["Pending"],
                    "section_depth": 1,
                    "section_idx": 0,
                    "revision": 0,
                }
            ),
        },
    )

    async def prepare(*args):
        phases.append("prepare")
        return plan

    async def ordinary(*args):
        phases.append("ordinary")
        return None

    async def get(*args):
        phases.append("checked_document")
        return checked

    async def chunks(*args, **kwargs):
        phases.append("checked_chunks")
        return [chunk]

    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(documents, "get_one", AsyncMock(side_effect=ordinary))
    monkeypatch.setattr(source_documents, "get", AsyncMock(side_effect=get))
    monkeypatch.setattr(source_documents, "chunks", AsyncMock(side_effect=chunks))
    monkeypatch.setattr(app, "get_embedder", lambda: SimpleNamespace(model_name="fixture"))
    return input, plan, phases


async def test_outline_prepares_before_content_and_uses_checked_unembedded_sections(world):
    input, _plan, phases = world
    result = await app.handle_knowledge_outline(input)
    assert result.found and result.title == "Checked title" and result.structured
    assert result.chunk_count == 1 and result.section_content == "Unembedded section."
    assert result.section_chunk_idxs == [1] and result.source_uri == input.source_uri
    assert result.sections[0].path == ["Pending"]
    assert phases == ["prepare", "ordinary", "checked_document", "checked_chunks"]


async def test_failed_checked_chunks_have_the_complete_missing_outline_reply(world):
    input, plan, _phases = world
    source_documents.chunks.side_effect = None
    source_documents.chunks.return_value = None
    failed = await app.handle_knowledge_outline(input)
    plan.marked = False
    absent = await app.handle_knowledge_outline(input)
    assert failed.model_dump(mode="json") == absent.model_dump(mode="json")
    assert not failed.found


async def test_valid_empty_checked_document_is_found(world):
    input, _plan, _phases = world
    source_documents.chunks.side_effect = None
    source_documents.chunks.return_value = []
    result = await app.handle_knowledge_outline(input)
    assert result.found and not result.structured and result.chunk_count == 0


async def test_ordinary_outline_wins_as_a_complete_copy(world, monkeypatch):
    input, _plan, phases = world
    documents.get_one.side_effect = None
    documents.get_one.return_value = SimpleNamespace(title="Ordinary title")
    row = SimpleNamespace(chunk_idx=0, content="Ordinary section.", chunk_metadata="{}")
    monkeypatch.setattr(chunk_store, "list_for_doc", AsyncMock(return_value=[row]))
    monkeypatch.setattr(app.pg_serving, "keep_served", AsyncMock(return_value=([row], 0)))
    result = await app.handle_knowledge_outline(input)
    assert result.found and result.title == "Ordinary title" and result.chunk_count == 1
    source_documents.get.assert_not_awaited()
    source_documents.chunks.assert_not_awaited()
    assert phases == ["prepare"]
