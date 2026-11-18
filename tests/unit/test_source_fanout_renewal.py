"""Wider knowledge failures remain additive during permission renewal."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.core import request_tail
from server.core.diagnostics import private_work
from server.knowledge.api.models_conversations import ConversationSearchOutput
from server.knowledge.api.models_knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)
from server.knowledge.app import repo_docs, source_publication
from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_corpus import LostCorpus
from server.memory.api.models_search import MemorySearchOutput
from server.memory.app.loom_search import fanout
from server.memory.domain.assistant_rules import hits_to_ask


def hit(uri, *, other=False):
    return _SearchHit(
        source_uri=uri,
        content=uri,
        chunk_idx=0,
        distance=0.2,
        similarity=0.8,
        score=0.8,
        namespace="home",
        tags=["source-kind:assistant_turn", "agent:other-bot"] if other else [],
    )


def result(name, *, lost=False, fail_render=False, needs_widen=False, ordinary_full=False):
    inp = KnowledgeSearchInput(org="acme", namespace="home", query="cache", top_k=2)
    request = repo_docs.RepoSearchRequest(
        SearchRequest(inp, ("home",), ()), None, "", asyncio.get_running_loop().time() + 100
    )
    mixed = KnowledgeSearchOutput(
        results=[hit("private-other", other=True), hit("private-a")]
        if needs_widen
        else [hit("private-" + name)],
        embedding_model="private-model",
        reranked_by="private-rank",
    )
    ordinary = KnowledgeSearchOutput(
        results=[hit("ordinary-a"), hit("ordinary-b")]
        if ordinary_full
        else [hit("ordinary-other", other=True), hit("ordinary-a")],
        embedding_model="ordinary-model",
        stale_dropped=7,
    )

    async def render(*_args):
        await asyncio.sleep(0)
        if fail_render:
            raise RuntimeError("PRIVATE-WIDER-ERROR")
        return ordinary

    proof = SimpleNamespace(
        mirrored=True,
        check=AsyncMock(side_effect=LostCorpus() if lost else None),
        fresh=lambda: not lost,
    )
    renderer = SimpleNamespace(marked=True, render=AsyncMock(side_effect=render))
    checked = CheckedSearch(mixed, proof, renderer, [], 7)
    return repo_docs.RepoSearch(request, checked, None), renderer


async def test_failed_wider_ordinary_render_does_not_cancel_first_fallback(caplog):
    first, first_renderer = result("first")
    wider, wider_renderer = result("wider", fail_render=True)
    with private_work():
        know = await fanout._ordinary_knowledge(first, wider, "reader")
    assert [h["source_uri"] for h in know["results"]] == ["ordinary-a"]
    assert (know["embedding_model"], know["reranked_by"], know["stale_dropped"]) == (
        "ordinary-model",
        "",
        7,
    )
    first_renderer.render.assert_awaited_once()
    wider_renderer.render.assert_awaited_once()
    assert "PRIVATE-WIDER-ERROR" not in caplog.text


async def test_late_wider_renewal_failure_keeps_ordinary_first_and_never_retries_wide(monkeypatch):
    first, first_renderer = result("first", needs_widen=True)
    wider, wider_renderer = result("wider", lost=True, fail_render=True)
    run = AsyncMock(return_value=wider)
    monkeypatch.setattr(repo_docs, "run_repo_search", run)
    with private_work():
        know = await fanout._knowledge_section(first, ReadPlan("acme", marked=True), "reader")
    assert [h["source_uri"] for h in know["results"]] == ["ordinary-a"]
    assert know["reranked_by"] == "" and know["stale_dropped"] == 7
    first_renderer.render.assert_awaited_once()
    wider_renderer.render.assert_awaited_once()
    run.assert_awaited_once()
    request, _plan = run.call_args.args
    assert request.base.input.top_k == hits_to_ask(2) and request.base.namespaces == ("home",)


async def test_independently_cancelled_knowledge_child_is_propagated(monkeypatch):
    first, _ = result("first")

    async def prepare(*_args):
        return first.request, ReadPlan("acme")

    async def cancelled(*_args):
        raise asyncio.CancelledError()

    async def memory(*_args, **_kwargs):
        return MemorySearchOutput(results=[], embedding_model="fake")

    async def empty(*_args, **_kwargs):
        return []

    async def conversations(*_args, **_kwargs):
        return ConversationSearchOutput(hits=[])

    async def namespaces(org, namespace, *_args, **_kwargs):
        return [namespace], []

    monkeypatch.setattr(fanout, "_prepare_knowledge", prepare)
    monkeypatch.setattr(fanout, "_run_knowledge", cancelled)
    monkeypatch.setattr(fanout, "memory_with_repo_decisions", memory)
    monkeypatch.setattr(fanout, "search_profile", empty)
    monkeypatch.setattr(fanout, "tracker_leg", empty)
    monkeypatch.setattr(fanout, "_conversation_leg", conversations)
    monkeypatch.setattr("server.auth.fanout.namespaces_to_search", namespaces)
    with pytest.raises(asyncio.CancelledError):
        await fanout.search_sections(
            org="acme",
            namespace="home",
            agent="reader",
            query="cache",
            top_k=2,
            tags=[],
            namespace_scope="this",
            recipe="",
            bodies="excerpt",
            page="",
            explain=False,
            repo_namespace="",
            tracker_plan=None,
        )


@pytest.mark.parametrize("reranked", [False, True])
@pytest.mark.parametrize("count_changed", [False, True])
@pytest.mark.parametrize("rerank_possible", [False, True])
async def test_displaced_base_keeps_only_surviving_private_metadata_dependencies(
    reranked, count_changed, rerank_possible
):
    first, _renderer = result("base")
    first.base.ordinary_rerank_possible = rerank_possible
    first.base.output.reranked_by = "private-ranker" if reranked else ""
    first.base._ordinary_dropped = 7 if count_changed else first.base.output.stale_dropped
    private = first.base.proofs
    extra = KnowledgeSearchOutput(
        results=[hit("ordinary-a"), hit("ordinary-b")], embedding_model="fake"
    )
    for item in extra.results:
        item.score = 1
    first.extra = CheckedSearch(extra, None, first.base._renderer, [], 0)
    sent = []

    async def app(scope, receive, send):
        output = await first.current()
        assert [item.source_uri for item in output.results] == ["ordinary-a", "ordinary-b"]
        for proof in first.publication_proofs:
            source_publication.retain(proof)
        private.check.side_effect = LostCorpus()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": output.model_dump_json().encode()})

    async def send(message):
        sent.append(message)

    if reranked or count_changed or rerank_possible:
        with pytest.raises(request_tail.RequiredTailError):
            await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
        assert sent == []
    else:
        await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
        assert b"ordinary-a" in sent[-1]["body"]


async def test_lost_first_with_full_ordinary_page_never_renews_unused_wider(monkeypatch):
    first, first_renderer = result("first", needs_widen=True, ordinary_full=True)
    first.base.proofs.check.side_effect = [None, LostCorpus()]
    wider, wider_renderer = result("wider", lost=True, fail_render=True)
    run = AsyncMock(return_value=wider)
    monkeypatch.setattr(repo_docs, "run_repo_search", run)
    with private_work():
        know = await fanout._knowledge_section(first, ReadPlan("acme", marked=True), "reader")
    assert [h["source_uri"] for h in know["results"]] == ["ordinary-a", "ordinary-b"]
    assert first.lost and not wider.lost
    first_renderer.render.assert_awaited_once()
    wider.base.proofs.check.assert_not_awaited()
    wider_renderer.render.assert_not_awaited()
    run.assert_awaited_once()


async def test_expired_extra_can_remove_widen_need_without_losing_the_first_base(monkeypatch):
    from dataclasses import replace

    first, first_renderer = result("first")
    first.base.output.results = [hit("private-a"), hit("private-b")]
    extra, _extra_renderer = result("extra")
    other = hit("extra-other", other=True)
    other.namespace, other.score = "repo", 0.99
    extra.base.output.results = [other]
    first.extra = extra.base
    extra_request = SearchRequest(
        first.request.base.input.model_copy(
            update={"namespace": "repo", "namespace_scope": "this", "rerank": "none"}
        ),
        ("repo",),
        (),
    )
    first.request = replace(first.request, extra=extra_request, repo_namespace="repo")
    wider, wider_renderer = result("wider", lost=True, fail_render=True)

    async def run(request, plan):
        wider.request = request
        first.request = replace(first.request, extra_deadline=asyncio.get_running_loop().time() - 1)
        return wider

    monkeypatch.setattr(repo_docs, "run_repo_search", run)
    with private_work():
        know = await fanout._knowledge_section(first, ReadPlan("acme", marked=True), "reader")
    assert [h["source_uri"] for h in know["results"]] == ["private-a", "private-b"]
    assert first.extra is None and not first.lost
    wider.base.proofs.check.assert_not_awaited()
    wider_renderer.render.assert_not_awaited()
    first_renderer.render.assert_not_awaited()
