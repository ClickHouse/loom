"""Search renewal owns one fallback, and span helpers preserve proof loss."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeSearchOutput
from server.knowledge.app import knowledge_search as app
from server.knowledge.app.search import ChunkHit
from server.knowledge.app.source_corpus import LostCorpus


@pytest.mark.parametrize(
    "failure", [RuntimeError("provider unavailable"), asyncio.CancelledError()]
)
async def test_failed_ordinary_fallback_is_never_retried_or_replaced_by_the_old_reply(failure):
    initial = KnowledgeSearchOutput(results=[], embedding_model="fake-model")
    renderer = SimpleNamespace(marked=True, render=AsyncMock(side_effect=failure))
    proofs = SimpleNamespace(check=AsyncMock(side_effect=LostCorpus()))
    result = app.CheckedSearch(initial, proofs, renderer, [], 0)
    with pytest.raises(type(failure)):
        await result.current()
    with pytest.raises(LostCorpus, match="^document access changed$"):
        await result.current()
    renderer.render.assert_awaited_once()
    assert result.marked


async def test_concurrent_renewals_render_and_cache_one_complete_ordinary_fallback():
    initial = KnowledgeSearchOutput(results=[], embedding_model="fake-model")
    fallback = KnowledgeSearchOutput(results=[], embedding_model="fake-model", stale_dropped=7)
    entered, release = asyncio.Event(), asyncio.Event()

    async def render(*_args):
        entered.set()
        await release.wait()
        return fallback

    renderer = SimpleNamespace(marked=True, render=AsyncMock(side_effect=render))
    proofs = SimpleNamespace(check=AsyncMock(side_effect=LostCorpus()))
    result = app.CheckedSearch(initial, proofs, renderer, [], 7)
    first = asyncio.create_task(result.current())
    await entered.wait()
    second = asyncio.create_task(result.current())
    await asyncio.sleep(0)
    renderer.render.assert_awaited_once_with([], 7)
    release.set()
    assert await first is await second is await result.current() is fallback
    renderer.render.assert_awaited_once()


async def test_forced_ordinary_fallback_is_cached_without_readmitting_valid_mirrors():
    initial = KnowledgeSearchOutput(results=[], embedding_model="fake-model")
    fallback = KnowledgeSearchOutput(results=[], embedding_model="fake-model", stale_dropped=7)
    renderer = SimpleNamespace(marked=True, render=AsyncMock(return_value=fallback))
    proofs = SimpleNamespace(check=AsyncMock())
    result = app.CheckedSearch(initial, proofs, renderer, [], 7)
    first, second = await asyncio.gather(result.ordinary(), result.current())
    assert first is second is await result.ordinary() is fallback
    assert result.lost and result.proofs is None
    proofs.check.assert_not_awaited()
    renderer.render.assert_awaited_once_with([], 7)


async def test_initially_ordinary_reply_needs_no_additional_provider_or_query_work():
    initial = KnowledgeSearchOutput(results=[], embedding_model="fake-model")
    renderer = SimpleNamespace(
        marked=True, render=AsyncMock(side_effect=AssertionError("no retry"))
    )
    result = app.CheckedSearch(initial, None, renderer, [], 0)
    assert await result.ordinary() is await result.current() is initial
    assert not result.lost
    renderer.render.assert_not_awaited()


@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize("lost", ["before", "during", "never"])
async def test_each_span_embedding_is_guarded_and_proof_loss_is_not_swallowed(
    monkeypatch, many, lost
):
    from server.memory.app import spans

    state = {"lost": lost == "before", "calls": 0}

    async def check():
        if state["lost"]:
            raise LostCorpus()

    async def embed(_text):
        state["calls"] += 1
        if lost == "during":
            state["lost"] = True
        return [1.0]

    async def embed_many(_texts):
        state["calls"] += 1
        if lost == "during":
            state["lost"] = True
        return [[1.0], [1.0]]

    async def best_spans(_query, _chunks, batch):
        await batch(["window one", "window two"])
        return [SimpleNamespace(text="Nearest span", start=0, end=12, similarity=1.0)]

    monkeypatch.setattr(spans, "best_spans", best_spans)
    embedder = SimpleNamespace(embed=embed)
    if many:
        embedder.embed_many = embed_many
    proofs = SimpleNamespace(check=AsyncMock(side_effect=check))
    [hit] = app._as_search_hits(
        [
            ("team", [ChunkHit("team", "gdrive:private", 0, "Private chunk", [], 0.2)]),
        ]
    )
    if lost == "never":
        await app._narrow_spans(embedder, [1.0], [hit], proofs=proofs)
        assert hit.span == "Nearest span" and state["calls"] == (1 if many else 2)
        assert proofs.check.await_count == (2 if many else 4)
    else:
        with pytest.raises(LostCorpus):
            await app._narrow_spans(embedder, [1.0], [hit], proofs=proofs)
        assert not hit.span and state["calls"] == (0 if lost == "before" else 1)
