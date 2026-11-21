"""Public listing prepares access before I/O and merges complete public families."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeListInput
from server.knowledge.app import document_reads as app
from server.knowledge.app import source_access, source_listing
from server.knowledge.app.source_list_proofs import ListProofs
from server.knowledge.store import documents


@pytest.fixture
def world(monkeypatch):
    calls = []
    state = SimpleNamespace(ordinary=[], mirrored=[], marked=True)

    def row(uri, stamp, *, namespace="team", title="Title"):
        return {
            "namespace": namespace,
            "source_uri": uri,
            "title": title,
            "description": "Description",
            "tags": ["wanted"],
            "ingested_at": datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=stamp),
            "superseded_at": None,
            "chunk_count": stamp,
            "content_preview": "Preview",
            "blob_path": "",
            "blob_size": 0,
            "blob_content_type": "",
            "original_filename": "",
        }

    async def prepare(org, names):
        calls.append("prepare")
        assert (org, names) == ("acme", ["team"])
        return SimpleNamespace(org=org, marked=state.marked)

    async def ordinary(*args, **kwargs):
        calls.append("ordinary")
        return state.ordinary

    async def mirrored(*args, **kwargs):
        calls.append("mirror")
        assert kwargs["namespaces"] == ["team"]
        return source_listing.Listing(
            state.mirrored, ListProofs(source_access.ReadPlan("acme"), False, ())
        )

    monkeypatch.setattr(app, "readable_namespaces", AsyncMock(return_value=["team"]))
    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(documents, "list_with_chunk_counts", AsyncMock(side_effect=ordinary))
    monkeypatch.setattr(source_listing, "collect", AsyncMock(side_effect=mirrored))
    return state, calls, row


async def test_prepare_precedes_ordinary_listing_then_checked_families_are_merged(world):
    state, calls, row = world
    state.ordinary = [row("ordinary", 80)]
    state.mirrored = [row("mirror", 100)]
    result = await app.handle_knowledge_list(KnowledgeListInput(org="acme", namespace="team"))
    assert calls == ["prepare", "ordinary", "mirror"]
    assert [(r.source_uri, r.chunk_count) for r in result.rows] == [
        ("mirror", 100),
        ("ordinary", 80),
    ]


async def test_ordinary_copy_wins_whole_family_before_limit_and_cursor(world):
    state, _calls, row = world
    state.ordinary = [row("same", 80, title="Ordinary")]
    state.mirrored = [row("same", 100, title="Mirror"), row("other", 70)]
    result = await app.handle_knowledge_list(
        KnowledgeListInput(org="acme", namespace="team", limit=1)
    )
    assert [(r.source_uri, r.title, r.chunk_count) for r in result.rows] == [
        ("same", "Ordinary", 80)
    ]
    assert result.next_cursor is not None
    at, ns, uri = app._list_position(result.next_cursor, "acme", ["team"])
    assert (at.replace(tzinfo=UTC), ns, uri) == (state.ordinary[0]["ingested_at"], "team", "same")


async def test_public_field_projection_applies_after_merge_and_retains_cursor(world):
    state, _calls, row = world
    state.mirrored = [row("mirror", 100), row("next", 80)]
    result = await app.handle_knowledge_list(
        KnowledgeListInput(org="acme", namespace="team", limit=1, fields=["title"])
    )
    assert result.model_dump()["rows"] == [
        {"namespace": "team", "source_uri": "mirror", "title": "Title"}
    ]
    assert result.next_cursor is not None


async def test_unmarked_plan_preserves_ordinary_listing_and_skips_checked_leg(world):
    state, calls, row = world
    state.marked = False
    state.ordinary = [row("ordinary", 80)]
    result = await app.handle_knowledge_list(KnowledgeListInput(org="acme", namespace="team"))
    assert [r.source_uri for r in result.rows] == ["ordinary"]
    assert calls == ["prepare", "ordinary"]
    source_listing.collect.assert_not_awaited()
