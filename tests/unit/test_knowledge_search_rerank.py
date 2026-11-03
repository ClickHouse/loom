"""knowledge.search's rerank stage: which reranker a call uses, what each candidate
shows it, and that a failed rerank keeps the fused order. CH-free."""

from __future__ import annotations

from typing import Any

import pytest

from server.knowledge.api.models_knowledge import KnowledgeSearchInput, _SearchHit
from server.knowledge.app import knowledge_search as ks
from server.memory.app import rerank as rerank_mod
from server.settings import settings


def _input(**kw: Any) -> KnowledgeSearchInput:
    return KnowledgeSearchInput(org="o", namespace="n", query="refunds", **kw)


def _hit(uri: str, idx: int = 0) -> _SearchHit:
    return _SearchHit(
        source_uri=uri,
        chunk_idx=idx,
        content=f"text of {uri}",
        tags=[],
        distance=0.5,
        similarity=0.5,
        namespace="n",
    )


def test_the_request_names_the_reranker_else_the_deployment_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "knowledge_search_rerank_default", "builtin:openai")
    assert ks._rerank_endpoint(_input()) == "builtin:openai"
    assert ks._rerank_endpoint(_input(rerank="none")) == ""
    assert ks._rerank_endpoint(_input(rerank="builtin:cohere")) == "builtin:cohere"
    monkeypatch.setattr(settings, "knowledge_search_rerank_default", "")
    assert ks._rerank_endpoint(_input()) == ""


def test_a_url_is_no_reranker_a_request_may_name() -> None:
    with pytest.raises(ValueError):
        _input(rerank="https://example.com/rerank")


@pytest.mark.asyncio
async def test_the_head_takes_the_rerankers_order_and_shows_each_chunks_context(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "knowledge_search_rerank_top", 2)
    monkeypatch.setattr(settings, "knowledge_search_rerank_excerpt_chars", 30)
    seen: dict[str, Any] = {}

    async def _reversed(**kw: Any) -> list[Any]:
        seen.update(kw)
        return list(reversed(kw["candidates"]))

    monkeypatch.setattr(rerank_mod, "rerank", _reversed)
    hits = [_hit("a.md"), _hit("b.md"), _hit("c.md")]
    contexts = {("n", "a.md", 0): "Lantern handbook > Refunds"}
    out, reranked = await ks._rerank("builtin:openai", "refunds", hits, contexts)
    assert reranked is True
    # the head of two is reversed; the third stays after it
    assert [h.source_uri for h in out] == ["b.md", "a.md", "c.md"]
    excerpts = [c.content_excerpt for c in seen["candidates"]]
    assert excerpts == ["Lantern handbook > Refunds\n\nte", "text of b.md"]
    assert seen["endpoint"] == "builtin:openai" and seen["query"] == "refunds"


@pytest.mark.asyncio
async def test_a_failed_rerank_keeps_the_fused_order_and_says_none_ran(monkeypatch) -> None:
    async def _kept(**kw: Any) -> list[Any]:
        return kw["candidates"]  # the service hands its input back on any failure

    monkeypatch.setattr(rerank_mod, "rerank", _kept)
    hits = [_hit("a.md"), _hit("b.md")]
    out, reranked = await ks._rerank("builtin:openai", "q", hits, {})
    assert reranked is False and out is hits


@pytest.mark.asyncio
async def test_the_search_reads_a_rerank_pool_and_reports_the_reranker(monkeypatch) -> None:
    """The legs fetch the rerank head even when the page is smaller, the reranked order
    is the page, and `reranked_by` names the reranker."""
    from server.knowledge.app.search import ChunkHit

    monkeypatch.setattr(settings, "knowledge_search_rerank_top", 4)
    pools: list[int] = []

    class _Embedder:
        model_name = "m"

        async def embed(self, text: str) -> list[float]:
            return [1.0]

    def _search(input: Any, embedder: Any, vec: Any, pool: int):
        pools.append(pool)

        async def _run(namespaces: list[str]) -> list[tuple[str, list[ChunkHit]]]:
            hits = [
                ChunkHit("n", f"{c}.md", 0, c, [], distance=0.1 * i, context=f"ctx {c}")
                for i, c in enumerate("abcd")
            ]
            return [("n", hits)]

        return _run

    async def _first(input: Any, namespaces: list[str], search: Any) -> Any:
        return await search(namespaces)

    async def _named(input: Any) -> list[str]:
        return ["n"]

    async def _reversed(**kw: Any) -> list[Any]:
        assert kw["candidates"][0].content_excerpt.startswith("ctx a\n\n")
        return list(reversed(kw["candidates"]))

    monkeypatch.setattr(ks, "get_embedder", _Embedder)
    monkeypatch.setattr(ks, "_namespaces_search", _search)
    monkeypatch.setattr(ks, "_search_private_first", _first)
    monkeypatch.setattr(ks, "_named_namespaces", _named)
    monkeypatch.setattr(rerank_mod, "rerank", _reversed)
    out = await ks.handle_knowledge_search(
        _input(rerank="builtin:openai", top_k=2, namespaces=["n"], collapse_sections=False)
    )
    assert pools == [4]
    assert [h.source_uri for h in out.results] == ["d.md", "c.md"]
    assert out.reranked_by == "builtin:openai"
    plain = await ks.handle_knowledge_search(
        _input(top_k=2, namespaces=["n"], collapse_sections=False)
    )
    assert pools[-1] == 2 and plain.reranked_by == ""
    assert [h.source_uri for h in plain.results] == ["a.md", "b.md"]
