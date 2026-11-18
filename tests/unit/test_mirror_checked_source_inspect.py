"""Source inspection reads only the requested source's checked title sample."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_sources import KnowledgeSourceInspectInput
from server.knowledge.app import source_access, source_titles
from server.knowledge.app import sources as app
from server.knowledge.domain.sources import KnowledgeSource

SID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch):
    events = []
    plan = SimpleNamespace(marked=True, failed=False, unread=False)
    src = KnowledgeSource(
        "acme",
        "team",
        "notes",
        "gdrive",
        SID,
        access="mirrored",
        created_by="owner",
        created_at=NOW,
    )

    async def prepare(org, namespaces):
        assert (org, namespaces) == ("acme", ["team"])
        events.append("prepare")
        return plan

    async def registry(*args):
        assert args == ("acme", "team", "notes")
        events.append("registry")
        return src

    async def states(*args):
        assert args == ("acme", "team", ["notes"])
        events.append("states")
        return {}

    async def titles(checked, namespace, *, limit, source_id):
        assert checked is plan and namespace == "team" and limit == 200 and source_id == SID
        events.append("titles")
        return [
            source_titles.Title(
                "gdrive:file#native",
                "Notes",
                "html",
                "gdrive",
                "r1",
                7,
                0,
                NOW,
                SimpleNamespace(mirrored=True, check=AsyncMock(), fresh=lambda: True),
            )
        ]

    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(app, "_source_or_404", AsyncMock(side_effect=registry))
    monkeypatch.setattr(app.store, "read_states", AsyncMock(side_effect=states))
    monkeypatch.setattr(
        app.store,
        "source_documents",
        AsyncMock(
            return_value=[
                {
                    "source_uri": "https://example.com/ordinary",
                    "title": "Ordinary",
                    "ingested_at": NOW,
                },
            ]
        ),
    )
    monkeypatch.setattr(source_titles, "collect", AsyncMock(side_effect=titles))
    return plan, src, events


async def inspect():
    return await app.handle_knowledge_source_inspect(
        KnowledgeSourceInspectInput(org="acme", namespace="team", name="notes")
    )


async def test_mirror_source_inspection_checks_own_titles_after_public_registry_state(world):
    output = await inspect()
    assert world[2] == ["prepare", "registry", "states", "titles"]
    assert output.name == "notes" and output.created_by == "owner"
    assert output.config == {}
    assert [doc.model_dump() for doc in output.documents] == [
        {
            "source_uri": "gdrive:file#native",
            "title": "Notes",
            "source_revision": "r1",
            "ingested_at": NOW.isoformat(),
        }
    ]
    app.store.source_documents.assert_not_awaited()


async def test_ordinary_source_inspection_keeps_its_existing_document_lookup(world, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(
        app, "_source_or_404", AsyncMock(return_value=replace(world[1], access="namespace"))
    )
    output = await inspect()
    assert [doc.source_uri for doc in output.documents] == ["https://example.com/ordinary"]
    app.store.source_documents.assert_awaited_once_with("acme", "team", "source:notes")
    source_titles.collect.assert_not_awaited()
    assert world[2] == ["prepare", "states"]


async def test_denied_titles_do_not_expose_document_count_or_status(world):
    world[0].failed = True
    source_titles.collect.side_effect = None
    source_titles.collect.return_value = []
    output = await inspect()
    assert output.documents == []
    assert output.sync.status == ""
    app.store.source_documents.assert_not_awaited()


async def test_source_title_cancellation_propagates(world):
    source_titles.collect.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await inspect()
