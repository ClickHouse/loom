"""Outer list renewal rebuilds a complete ordinary page and its cursor once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeListInput
from server.knowledge.app import document_reads as app
from server.knowledge.app import source_access, source_listing
from server.knowledge.app.source_access import Admitted, ReadPlan
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.app.source_list_proofs import ListProofs
from server.knowledge.domain.access_list import AccessList
from server.knowledge.store import documents, source_documents

NS = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def world(monkeypatch):
    now = datetime(2025, 1, 1, tzinfo=UTC)
    state = SimpleNamespace(allowed=True, marked=True, denied=set())
    plan = ReadPlan(
        "acme", marked=True, sources={(NS, SID): "team"}, listing_starts={(NS, SID): now}
    )
    monkeypatch.setattr(source_access, "now", lambda: now)

    def row(uri, stamp):
        return {
            "namespace": "team",
            "source_uri": uri,
            "title": uri,
            "description": uri + " description",
            "tags": [],
            "ingested_at": now + timedelta(seconds=stamp),
            "superseded_at": None,
            "chunk_count": stamp,
            "content_preview": uri + " preview",
        }

    ordinary = [row(f"ordinary:{i}", 80 - i) for i in range(3)]
    mirrored = [row(f"gdrive:{i}", 100 - i) for i in range(2)]
    items = {
        r["source_uri"] + "#" + SID: Admitted(
            "team",
            {"namespace": NS, "source_uri": r["source_uri"] + "#" + SID},
            {
                **r,
                "namespace": NS,
                "source_uri": r["source_uri"] + "#" + SID,
                "tags": ["source-id:" + SID],
                "version": 10,
                "revision": 0,
            },
            AccessList(anyone=True),
        )
        for r in mirrored
    }

    async def check(candidates):
        return (
            [
                items[r["source_uri"]]
                for r in candidates
                if r["source_uri"].split("#", 1)[0] not in state.denied
            ]
            if state.allowed
            else []
        )

    async def prepare(org, namespaces):
        assert org == "acme" and namespaces == ["team"]
        plan.marked = state.marked
        return plan

    async def collect(actual, input, *, namespaces, cursor):
        assert actual is plan and namespaces == ["team"] and cursor is None
        if not state.allowed:
            return source_listing.Listing([], ListProofs(plan, input.include_superseded, ()))
        proofs = ListProofs(plan, input.include_superseded, tuple(items.values()))
        await proofs.check()
        return source_listing.Listing(mirrored, proofs)

    monkeypatch.setattr(plan, "check", AsyncMock(side_effect=check))
    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(app, "readable_namespaces", AsyncMock(return_value=["team"]))
    monkeypatch.setattr(documents, "list_with_chunk_counts", AsyncMock(return_value=ordinary))
    monkeypatch.setattr(source_documents, "ordinary_families", AsyncMock(return_value=set()))
    monkeypatch.setattr(source_listing, "collect", AsyncMock(side_effect=collect))
    return state, plan, ordinary


def request(**kwargs):
    return KnowledgeListInput(org="acme", namespace="team", limit=2, **kwargs)


@pytest.mark.parametrize("fields", [None, ["title"]])
async def test_late_loss_restores_the_whole_ordinary_page_projection_and_cursor(world, fields):
    state, _plan, ordinary = world
    options = {"fields": fields} if fields is not None else {}
    checked = await app.checked_knowledge_list(request(**options))
    assert [r.source_uri for r in checked.output.rows] == ["gdrive:0", "gdrive:1"]
    mixed_cursor = checked.output.next_cursor
    checked.output.rows[0].title = "Changed provisional title"
    calls = documents.list_with_chunk_counts.await_count
    state.allowed = False
    first, second = await asyncio.gather(checked.current(), checked.current())
    assert first is second is await checked.current() is await checked.ordinary()
    assert checked.lost and checked.proofs is None
    assert documents.list_with_chunk_counts.await_count == calls
    baseline = await app.handle_knowledge_list(request(**options))
    assert first.model_dump() == baseline.model_dump()
    assert [r.source_uri for r in first.rows] == [r["source_uri"] for r in ordinary[:2]]
    assert first.next_cursor is not None and first.next_cursor != mixed_cursor


async def test_forced_ordinary_reply_is_cached_and_never_readmits_still_valid_mirrors(world):
    _state, plan, _ordinary = world
    checked = await app.checked_knowledge_list(request())
    calls = plan.check.await_count
    reply = await checked.ordinary()
    assert checked.lost and checked.proofs is None
    assert reply is await checked.current() is await checked.ordinary()
    assert plan.check.await_count == calls
    assert all(r.source_uri.startswith("ordinary:") for r in reply.rows)


async def test_an_initially_ordinary_result_needs_no_render_or_proof_retry(world, monkeypatch):
    state, plan, _ordinary = world
    state.allowed = False
    checked = await app.checked_knowledge_list(request())
    render = Mock(side_effect=AssertionError("ordinary result already rendered"))
    monkeypatch.setattr(app._ListRenderer, "render", render)
    assert await checked.ordinary() is await checked.current() is checked.output
    assert not checked.lost
    plan.check.assert_not_awaited()
    render.assert_not_called()


async def test_failed_fallback_cannot_return_the_old_private_page_or_retry(world, monkeypatch):
    state, _plan, _ordinary = world
    checked = await app.checked_knowledge_list(request())
    state.allowed = False
    render = Mock(side_effect=RuntimeError("ordinary renderer failed"))
    monkeypatch.setattr(app._ListRenderer, "render", render)
    with pytest.raises(RuntimeError, match="ordinary renderer failed"):
        await checked.current()
    for method in (checked.current, checked.ordinary):
        with pytest.raises(LostCorpus):
            await method()
    render.assert_called_once()


async def test_cancelled_renewal_propagates_without_returning_a_page(world):
    _state, plan, _ordinary = world
    checked = await app.checked_knowledge_list(request())
    plan.check.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await checked.current()
    assert not checked.lost and not plan.failed and not plan.unread


async def test_recipe_snapshots_inputs_and_executes_its_resolved_scope_without_resolving_again(
    world,
):
    _state, plan, _ordinary = world
    original = request(fields=["title"], namespaces=["team"])
    recipe = await app.resolve_list(original)
    original.namespaces.append("another-team")
    original.fields.append("content_preview")
    app.readable_namespaces.side_effect = AssertionError("scope already resolved")
    result = await app.run_checked_list(recipe, plan)
    assert recipe.namespaces == ("team",) and recipe.input.fields == ["title"]
    assert set(result.output.model_dump()["rows"][0]) == {"namespace", "source_uri", "title"}
    source_access.prepare.assert_not_awaited()


async def test_execution_refuses_another_orgs_plan_before_any_document_read(world):
    recipe = await app.resolve_list(request())
    with pytest.raises(ValueError, match="document read scope changed"):
        await app.run_checked_list(recipe, ReadPlan("another-org"))
    documents.list_with_chunk_counts.assert_not_awaited()
    source_listing.collect.assert_not_awaited()


async def test_unused_mirror_beyond_merged_page_and_lookahead_cannot_invalidate_page(world):
    state, _plan, ordinary = world
    ordinary[0]["ingested_at"] += timedelta(seconds=19, milliseconds=500)
    checked = await app.checked_knowledge_list(
        KnowledgeListInput(org="acme", namespace="team", limit=1)
    )
    assert checked.output.rows[0].source_uri == "gdrive:0" and checked.output.next_cursor
    state.denied.add("gdrive:1")
    assert await checked.current() is checked.output
    assert checked.output.rows[0].source_uri == "gdrive:0" and not checked.lost


async def test_private_cursor_lookahead_is_still_a_dependency(world):
    state, _plan, _ordinary = world
    checked = await app.checked_knowledge_list(
        KnowledgeListInput(org="acme", namespace="team", limit=1)
    )
    assert checked.output.rows[0].source_uri == "gdrive:0" and checked.output.next_cursor
    state.denied.add("gdrive:1")
    output = await checked.current()
    assert checked.lost and output.rows[0].source_uri == "ordinary:0"


@pytest.mark.parametrize("shadow", [False, True])
async def test_wholly_ordinary_page_and_cursor_retain_no_displaced_mirror(world, shadow):
    state, plan, ordinary = world
    if shadow:
        ordinary[0]["source_uri"] = "gdrive:0"
    for row in ordinary:
        row["ingested_at"] += timedelta(seconds=100)
    checked = await app.checked_knowledge_list(request())
    assert checked.proofs is None
    assert [row.source_uri for row in checked.output.rows] == [
        row["source_uri"] for row in ordinary[:2]
    ]
    state.allowed = False
    count = plan.check.await_count
    assert await checked.current() is checked.output and not checked.lost
    assert plan.check.await_count == count
