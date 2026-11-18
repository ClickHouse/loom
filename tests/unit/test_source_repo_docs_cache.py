"""Mirrored repository listings bypass namespace caches before either cache access."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app import document_reads, repo_docs, source_access
from server.knowledge.app.source_list_proofs import ListProofs
from server.knowledge.domain.access_list import AccessList
from server.knowledge.store import source_documents
from server.settings import settings


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(allowed=True, marked=True, unread=False, phases=[])

    async def resolve(input):
        state.phases.append("resolve")
        return document_reads.ListRequest(input.model_copy(deep=True), ("code-kj",), None)

    async def prepare(org, namespaces):
        state.phases.append("prepare")
        assert (org, namespaces) == ("acme", ["code-kj"])
        return source_access.ReadPlan(org, marked=state.marked, unread=state.unread)

    async def run(request, plan):
        state.phases.append("list")
        assert request.namespaces == ("code-kj",) and plan.org == "acme"
        title = "Ordinary title" if not state.marked else "Current private title"
        rows = (
            [SimpleNamespace(source_uri="gdrive:file", title=title, description="Description")]
            if not state.marked or (state.allowed and not state.unread)
            else []
        )
        return SimpleNamespace(
            output=SimpleNamespace(rows=rows), marked=state.marked, lost=False, proofs=None
        )

    repo_docs.reset_cache_for_tests()
    monkeypatch.setattr(
        "server.code.app.repo_scope.readable_repo_namespace", AsyncMock(return_value="code-kj")
    )
    monkeypatch.setattr(document_reads, "resolve_list", AsyncMock(side_effect=resolve))
    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(document_reads, "run_checked_list", AsyncMock(side_effect=run))
    yield state
    repo_docs.reset_cache_for_tests()


async def test_private_titles_are_never_shared_with_a_second_reader_by_the_namespace_cache(world):
    first = (await repo_docs.list_repo_docs("acme", "code-kj")).docs
    assert first[0]["title"] == "Current private title"
    world.allowed = False
    second = (await repo_docs.list_repo_docs("acme", "code-kj")).docs
    assert second == []
    assert repo_docs._LIST_CACHE == {}
    assert world.phases == ["resolve", "prepare", "list"] * 2


@pytest.mark.parametrize(
    "cached", [[], [{"source_uri": "old", "title": "Old private title", "description": ""}]]
)
async def test_marked_listing_reads_neither_a_positive_nor_a_negative_cached_page(world, cached):
    key = ("acme", "code-kj", repo_docs.DOCS_SECTION_MAX_DOCS)
    repo_docs._LIST_CACHE[key] = (float("inf"), cached)
    docs = (await repo_docs.list_repo_docs("acme", "code-kj")).docs
    assert docs == [
        {
            "source_uri": "gdrive:file",
            "title": "Current private title",
            "description": "Description",
        }
    ]
    assert repo_docs._LIST_CACHE[key][1] == cached
    assert world.phases == ["resolve", "prepare", "list"]


async def test_unread_mirror_preparation_never_falls_back_to_cached_private_titles(world):
    key = ("acme", "code-kj", repo_docs.DOCS_SECTION_MAX_DOCS)
    repo_docs._LIST_CACHE[key] = (
        float("inf"),
        [{"source_uri": "old", "title": "Old private title", "description": ""}],
    )
    world.unread = True
    assert await repo_docs.list_repo_docs("acme", "code-kj") == repo_docs.RepoListing("code-kj", [])
    assert world.phases == ["resolve", "prepare", "list"]


async def test_ordinary_cache_still_prepares_the_current_caller_before_each_cache_lookup(world):
    world.marked = False
    first = await repo_docs.list_repo_docs("acme", "code-kj")
    assert first == await repo_docs.list_repo_docs("acme", "code-kj")
    assert first.docs[0]["title"] == "Ordinary title"
    assert world.phases == ["resolve", "prepare", "list", "resolve", "prepare"]


async def test_preparation_is_inside_the_additive_listing_timeout(world, monkeypatch):
    async def wait(*_args):
        await asyncio.Future()

    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=wait))
    assert await repo_docs.list_repo_docs(
        "acme", "code-kj", timeout_s=0.01
    ) == repo_docs.RepoListing("code-kj", [])
    assert repo_docs._LIST_CACHE == {}


async def test_private_failure_details_do_not_escape_the_additive_logger(
    world, monkeypatch, caplog
):
    monkeypatch.setattr(
        document_reads,
        "run_checked_list",
        AsyncMock(side_effect=ValueError("PRIVATE-SOURCE-CONTENT")),
    )
    with caplog.at_level("INFO", logger="loom.repo_docs"):
        assert await repo_docs.list_repo_docs("acme", "code-kj") == repo_docs.RepoListing(
            "code-kj", []
        )
    assert "PRIVATE-SOURCE-CONTENT" not in caplog.text
    assert repo_docs._LIST_CACHE == {}


async def test_permission_expiry_during_timeout_cleanup_returns_the_complete_ordinary_list(
    world, monkeypatch
):
    clock = SimpleNamespace(at=datetime(2025, 1, 1, tzinfo=UTC))
    mirror, sid = "acl-" + "1" * 24, "11111111-1111-4111-8111-111111111111"
    plan = source_access.ReadPlan("acme", marked=True, listing_starts={(mirror, sid): clock.at})
    ordinary = {
        "namespace": "code-kj",
        "source_uri": "ordinary:page",
        "title": "Ordinary title",
        "description": "Ordinary description",
        "tags": [],
        "ingested_at": clock.at,
        "superseded_at": None,
        "chunk_count": 1,
    }
    private = {
        **ordinary,
        "source_uri": "gdrive:file",
        "title": "Private title",
        "ingested_at": clock.at + timedelta(seconds=1),
    }
    stored = "gdrive:file#" + sid
    item = source_access.Admitted(
        "code-kj",
        {"namespace": mirror, "source_uri": stored},
        {
            **private,
            "namespace": mirror,
            "source_uri": stored,
            "tags": ["source-id:" + sid],
            "version": 10,
            "revision": 0,
        },
        AccessList(anyone=True),
    )
    plan.check = AsyncMock(return_value=[item])
    proof = ListProofs(plan, False, (item,))
    results = []

    async def run(request, actual_plan):
        assert actual_plan is plan
        await proof.check()
        renderer = document_reads._ListRenderer(request)
        checked = document_reads.CheckedList(
            renderer.render([ordinary], [private]), proof, True, renderer, [ordinary]
        )
        assert [r.source_uri for r in checked.output.rows] == ["gdrive:file"]
        results.append(checked)
        return checked

    @asynccontextmanager
    async def timeout(_seconds):
        yield
        clock.at += timedelta(seconds=6)

    monkeypatch.setattr(source_access, "now", lambda: clock.at)
    monkeypatch.setattr(settings, "knowledge_source_acl_stale_seconds", 5)
    monkeypatch.setattr(source_access, "prepare", AsyncMock(return_value=plan))
    monkeypatch.setattr(document_reads, "run_checked_list", run)
    monkeypatch.setattr(source_documents, "ordinary_families", AsyncMock(return_value=set()))
    monkeypatch.setattr(repo_docs.asyncio, "timeout", timeout)
    assert await repo_docs.list_repo_docs("acme", "code-kj", limit=1) == repo_docs.RepoListing(
        "code-kj",
        [
            {
                "source_uri": "ordinary:page",
                "title": "Ordinary title",
                "description": "Ordinary description",
            }
        ],
    )
    assert results[0].lost and results[0].proofs is None and proof.invalid
    assert repo_docs._LIST_CACHE == {}
