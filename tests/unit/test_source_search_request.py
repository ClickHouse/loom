"""Search recipes snapshot authorized scopes before shared-plan provider work."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.core.diagnostics import codes_only
from server.knowledge.api.models_knowledge import KnowledgeSearchInput
from server.knowledge.app import knowledge_search as app
from server.knowledge.app import source_access
from server.knowledge.app.search import ChunkHit


@pytest.fixture
def world(monkeypatch):
    import server.auth.gateway as gateway

    monkeypatch.setattr(gateway, "is_end_user", lambda _: False)
    readable = AsyncMock(return_value=["team"])
    embedder = SimpleNamespace(model_name="fake", embed=AsyncMock(return_value=[1.0]))
    ordinary = [("team", [ChunkHit("team", "ordinary:one", 0, "Ordinary", [], 0.1, score=0.9)])]
    monkeypatch.setattr(app, "readable_namespaces", readable)
    monkeypatch.setattr(app, "get_embedder", Mock(return_value=embedder))
    monkeypatch.setattr(app, "_search_private_first", AsyncMock(return_value=ordinary))
    monkeypatch.setattr(app, "_namespaces_search", Mock())
    monkeypatch.setattr(source_access, "prepare", AsyncMock())
    return readable, embedder


async def test_resolved_recipe_is_snapshotted_and_execution_never_resolves_scope_again(world):
    readable, embedder = world
    original = KnowledgeSearchInput(
        org="acme", namespace="@me", namespaces=["team"], query="Question", rerank="none"
    )
    recipe = await app.resolve_search(original)
    original.namespaces.append("another-team")
    original.query = "Changed"
    readable.side_effect = AssertionError("authorized scope already resolved")
    result = await app.run_checked_search(recipe, source_access.ReadPlan("acme"))
    assert recipe.namespaces == ("team",) and recipe.input.namespaces == ["team"]
    assert recipe.input.query == "Question" and not result.lost
    assert result.output.results[0].source_uri == "ordinary:one"
    embedder.embed.assert_awaited_once_with("Question")
    source_access.prepare.assert_not_awaited()
    assert await result.current() is await result.ordinary() is result.output


async def test_recipe_execution_refuses_another_org_before_providers_or_document_reads(world):
    _readable, embedder = world
    recipe = await app.resolve_search(
        KnowledgeSearchInput(org="acme", namespace="team", namespaces=["team"], query="Question")
    )
    with pytest.raises(ValueError, match="document read scope changed"):
        await app.run_checked_search(recipe, source_access.ReadPlan("another-org"))
    embedder.embed.assert_not_awaited()
    app._search_private_first.assert_not_awaited()


@pytest.mark.parametrize("marked", [False, True])
async def test_entire_recipe_execution_is_private_when_the_shared_plan_is_marked(
    world, monkeypatch, marked
):
    recipe = await app.resolve_search(
        KnowledgeSearchInput(org="acme", namespace="team", namespaces=["team"], query="Question")
    )
    expected = object()

    async def execute(actual, plan):
        assert actual is recipe and plan.org == "acme"
        assert codes_only() is marked
        return expected

    monkeypatch.setattr(app, "_run_search", execute)
    assert not codes_only()
    assert (
        await app.run_checked_search(recipe, source_access.ReadPlan("acme", marked=marked))
        is expected
    )
    assert not codes_only()
