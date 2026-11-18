"""Passage composition retains complete ordinary widths and terminal fallbacks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.knowledge.api.models_knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)
from server.knowledge.app import knowledge_search, source_passages
from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_corpus import LostCorpus


def hit(name, *, noise=False):
    return _SearchHit(
        namespace="docs",
        source_uri=name,
        chunk_idx=0,
        content=name,
        tags=[],
        distance=0.2,
        similarity=0.8,
        noise=noise,
    )


def names(hits):
    return [h.source_uri for h in hits]


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(
        queries=[],
        first=[hit("private")],
        wide=[hit("private-wide")],
        first_ordinary=[hit("ordinary-first")],
        wide_ordinary=[hit("ordinary-wide")],
        children={},
        proofs={},
        render_calls=[],
        render_gate=None,
        render_entered=asyncio.Event(),
        first_error=None,
        wide_error=None,
        wide_render_error=None,
        revoke=False,
    )

    class Proof:
        invalid = False

        async def check(self):
            if state.revoke:
                self.invalid = True
                raise LostCorpus()

        def fresh(self):
            return not self.invalid and not state.revoke

    class Renderer:
        marked = True

        def __init__(self, width):
            self.width = width

        async def render(self, rows, dropped):
            state.render_calls.append(self.width)
            state.render_entered.set()
            if state.render_gate is not None:
                await state.render_gate.wait()
            if self.width > 2 and state.wide_render_error:
                raise state.wide_render_error
            values = state.first_ordinary if self.width == 2 else state.wide_ordinary
            return KnowledgeSearchOutput(results=values, embedding_model="fake")

    async def run(request, plan):
        state.queries.append(request)
        width = request.input.top_k
        error = state.first_error if width == 2 else state.wide_error
        if error is not None:
            raise error
        values = state.first if width == 2 else state.wide
        renderer = Renderer(width)
        renderer.marked = plan.marked
        proof = Proof() if plan.marked else None
        checked = CheckedSearch(
            KnowledgeSearchOutput(results=values, embedding_model="fake"), proof, renderer, [], 0
        )
        state.children[width], state.proofs[width] = checked, proof
        return checked

    monkeypatch.setattr(knowledge_search, "run_checked_search", run)
    return state


def request():
    return SearchRequest(
        KnowledgeSearchInput(
            org="acme",
            namespace="docs",
            query="question",
            top_k=2,
            namespace_scope="all_mine",
            rerank="none",
            tags=["hard"],
        ),
        ("docs", "other"),
        ("unread",),
    )


async def collect(world, *, marked=True):
    return await source_passages.collect(
        request(), ReadPlan("acme", marked=marked), "reader", keep=lambda h: not h.noise
    )


@pytest.mark.parametrize("values", [[], [hit("private")], [hit("private", noise=True)]])
async def test_marked_work_keeps_true_k_and_wide_queries_even_without_a_drop(world, values):
    world.first = values
    checked = await collect(world)
    assert [q.input.top_k for q in world.queries] == [2, 8]
    assert all(
        q.namespaces == ("docs", "other")
        and q.skipped == ("unread",)
        and q.input.tags == ["hard"]
        and q.input.rerank == "none"
        for q in world.queries
    )
    assert names(await checked.current()) == (
        ["private-wide"] if values and values[0].noise else names(values)
    )
    assert checked.marked and not checked.lost


@pytest.mark.parametrize("dropped,expected", [(False, [2]), (True, [2, 8])])
async def test_unmarked_widening_stays_lazy(world, dropped, expected):
    world.first = [hit("first", noise=dropped)]
    checked = await collect(world, marked=False)
    assert [q.input.top_k for q in world.queries] == expected
    assert names(await checked.current()) == (["private-wide"] if dropped else ["first"])
    assert [q.input.top_k for q in world.queries] == expected
    assert not checked.marked


async def test_loss_reselects_both_complete_saved_ordinary_widths(world):
    world.first = [hit("private")]
    world.first_ordinary = [hit("ordinary-noise", noise=True), hit("ordinary-k")]
    world.wide_ordinary = [hit("ordinary-w1"), hit("ordinary-w2")]
    checked = await collect(world)
    assert names(checked.hits) == ["private"]
    world.revoke = True
    assert names(await checked.current()) == ["ordinary-w1", "ordinary-w2"]
    assert checked.lost and checked.fresh()
    assert world.render_calls == [2, 8]
    assert names(await checked.current()) == ["ordinary-w1", "ordinary-w2"]
    assert names(await checked.ordinary()) == ["ordinary-w1", "ordinary-w2"]
    assert world.render_calls == [2, 8]
    assert [q.input.top_k for q in world.queries] == [2, 8]


async def test_unused_wide_proof_loss_still_forces_the_complete_ordinary_pair(world):
    checked = await collect(world)
    world.proofs[8].invalid = True
    assert names(await checked.current()) == ["ordinary-first"]
    assert checked.lost and world.render_calls == [2, 8]


async def test_first_failure_is_not_rescued_by_a_wide_pass(world):
    world.first_error = ValueError("first failed")
    with pytest.raises(ValueError, match="first failed"):
        await collect(world)
    assert [q.input.top_k for q in world.queries] == [2]


async def test_optional_wide_failure_does_not_retry_or_replace_first(world):
    world.first = [hit("keep"), hit("noise", noise=True)]
    world.wide_error = ValueError("wide failed")
    checked = await collect(world)
    assert names(await checked.current()) == ["keep"]
    assert names(await checked.current()) == ["keep"]
    assert [q.input.top_k for q in world.queries] == [2, 8]


async def test_optional_wide_ordinary_render_failure_keeps_first_and_is_not_retried(world):
    world.first_ordinary = [hit("keep"), hit("noise", noise=True)]
    checked = await collect(world)
    world.revoke, world.wide_render_error = True, ValueError("wide render failed")
    assert names(await checked.current()) == ["keep"]
    assert names(await checked.ordinary()) == ["keep"]
    assert world.render_calls == [2, 8] and checked.lost


async def test_pending_ordinary_fallback_is_never_fresh(world):
    checked = await collect(world)
    world.render_gate = asyncio.Event()
    task = asyncio.create_task(checked.ordinary())
    try:
        await asyncio.wait_for(world.render_entered.wait(), timeout=5)
        assert not checked.fresh()
        assert names(checked.hits) == ["private"]
        world.render_gate.set()
        assert names(await task) == ["ordinary-first"]
        assert checked.fresh() and checked.lost
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_canceled_fallback_is_terminal_and_never_reuses_private_hits(world):
    checked = await collect(world)
    world.render_gate = asyncio.Event()
    task = asyncio.create_task(checked.ordinary())
    try:
        await asyncio.wait_for(world.render_entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not checked.fresh()
        with pytest.raises(LostCorpus):
            await checked.current()
        with pytest.raises(LostCorpus):
            await checked.ordinary()
        assert world.render_calls == [2]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_wide_collection_cancellation_propagates(world):
    world.wide_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await collect(world)
    assert [q.input.top_k for q in world.queries] == [2, 8]


async def test_wrong_org_is_refused_before_any_child_work(world):
    with pytest.raises(ValueError, match="scope changed"):
        await source_passages.collect(request(), ReadPlan("other", marked=True), "reader")
    assert world.queries == []


async def test_failed_wide_collection_is_not_retried_when_access_is_later_lost(world):
    world.first = [hit("keep"), hit("noise", noise=True)]
    world.first_ordinary = [hit("ordinary-keep"), hit("ordinary-noise", noise=True)]
    world.wide_error = ValueError("wide failed")
    checked = await collect(world)
    assert names(checked.hits) == ["keep"]
    world.revoke = True
    assert names(await checked.current()) == ["ordinary-keep"]
    assert names(await checked.ordinary()) == ["ordinary-keep"]
    assert [q.input.top_k for q in world.queries] == [2, 8]
    assert world.render_calls == [2] and checked.lost and checked.fresh()
