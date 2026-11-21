"""Public document reads project checked copies while mining stays ordinary-only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeGetInput
from server.knowledge.app import document_reads as app
from server.knowledge.app import source_access, source_documents
from server.knowledge.app.source_access import Admitted
from server.knowledge.store import documents

SID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def world(monkeypatch):
    from datetime import UTC, datetime

    phases = []
    plan = SimpleNamespace(marked=True)
    input = KnowledgeGetInput(org="acme", namespace="team", source_uri="gdrive:file#native")
    row = documents.DocumentRow(
        org="acme",
        namespace="acl-" + "1" * 24,
        source_uri=input.source_uri + "#" + SID,
        doc_id="22222222-2222-4222-8222-222222222222",
        content="admitted content",
        title="Checked",
        tags=["source-id:" + SID, "kind:notes"],
        version=10,
        revision=0,
        ingested_at=datetime(2025, 1, 1, tzinfo=UTC),
        access_json="private ACL",
    )
    checked = source_documents.CheckedDocument(Admitted("team", {}, {}), row)

    async def prepare(*args):
        phases.append(("prepare", args))
        return plan

    async def ordinary(*args, **kwargs):
        phases.append(("ordinary", args, kwargs))
        return None

    async def mirrored(*args):
        phases.append(("mirrored", args))
        return checked

    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(documents, "get_one", AsyncMock(side_effect=ordinary))
    monkeypatch.setattr(source_documents, "get", AsyncMock(side_effect=mirrored))
    return input, row, plan, phases


async def test_get_prepares_before_any_content_and_projects_only_checked_copy(world):
    input, row, plan, phases = world
    result = await app.handle_knowledge_get(input)
    assert result.found and result.content == row.content and result.title == row.title
    assert result.source_uri == input.source_uri and result.tags == ["kind:notes"]
    assert "access_json" not in result.model_dump()
    assert phases == [
        ("prepare", ("acme", ["team"])),
        ("ordinary", ("acme", "team", input.source_uri), {"include_superseded": False}),
        ("mirrored", (plan, "team", input.source_uri)),
    ]


async def test_existing_ordinary_copy_wins_with_all_its_fields_unchanged(world):
    input, row, _plan, _phases = world
    ordinary = row.model_copy(
        update={
            "namespace": "team",
            "source_uri": input.source_uri,
            "content": "ordinary text",
            "tags": ["ordinary"],
        }
    )
    documents.get_one.side_effect = None
    documents.get_one.return_value = ordinary
    result = await app.handle_knowledge_get(input)
    assert result.found and result.content == "ordinary text" and result.tags == ["ordinary"]
    source_documents.get.assert_not_awaited()


@pytest.mark.parametrize("include_superseded", [False, True])
async def test_missing_and_denied_mirror_have_the_complete_missing_reply(world, include_superseded):
    input, _row, plan, _phases = world
    input = input.model_copy(update={"include_superseded": include_superseded})
    source_documents.get.side_effect = None
    source_documents.get.return_value = None
    hidden = await app.handle_knowledge_get(input)
    plan.marked = False
    missing = await app.handle_knowledge_get(input)
    assert hidden.model_dump(mode="json") == missing.model_dump(mode="json")
    assert not hidden.found and hidden.source_uri == input.source_uri


async def test_ordinary_helper_cannot_prepare_or_read_mirrors(world):
    input, _row, _plan, phases = world
    result = await app.ordinary_get(input)
    assert not result.found
    source_access.prepare.assert_not_awaited()
    source_documents.get.assert_not_awaited()
    assert len(phases) == 1 and phases[0][0] == "ordinary"


async def test_decision_citation_probe_uses_only_ordinary_document_reads(monkeypatch):
    from server.memory.app.decision_extract import cites_document, source_resource_id

    ordinary = AsyncMock(return_value=SimpleNamespace(found=False))
    monkeypatch.setattr(app, "ordinary_get", ordinary, raising=False)
    mirrored = AsyncMock(side_effect=AssertionError("mining cannot read mirrored documents"))
    monkeypatch.setattr(app, "handle_knowledge_get", mirrored)
    uri = "gdrive:file"
    assert await cites_document(
        "acme", source_resource_id("team", uri, "part#heading"), "team", uri
    )
    ordinary.assert_awaited_once()
    mirrored.assert_not_awaited()
