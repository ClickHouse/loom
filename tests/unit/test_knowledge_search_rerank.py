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
    # "none" as the default is no reranker, and widens no pool
    monkeypatch.setattr(settings, "knowledge_search_rerank_default", "none")
    assert ks._rerank_endpoint(_input()) == ""


def test_an_llm_free_read_reranks_only_as_named_and_never_with_the_llm(monkeypatch) -> None:
    from server.core import llm_free

    monkeypatch.setattr(settings, "knowledge_search_rerank_default", "builtin:cohere")
    token = llm_free._read.set(True)
    try:
        assert ks._rerank_endpoint(_input()) == ""  # the default is a model call
        assert ks._rerank_endpoint(_input(rerank="builtin:openai")) == ""
        assert ks._rerank_endpoint(_input(rerank="builtin:cohere")) == "builtin:cohere"
    finally:
        llm_free._read.reset(token)


def test_a_default_no_request_may_name_reranks_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "knowledge_search_rerank_default", "https://example.com/r")
    assert ks._rerank_endpoint(_input()) == ""
    # a runtime override to Cohere while the gateway is required: boot never saw it
    monkeypatch.setattr(settings, "inference_gateway_required", True)
    monkeypatch.setattr(settings, "knowledge_search_rerank_default", "builtin:cohere")
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
    out, reranked = await ks._rerank("builtin:openai", "refunds", hits, contexts, 2)
    assert reranked is True
    # the head of two is reversed; the third stays after it
    assert [h.source_uri for h in out] == ["b.md", "a.md", "c.md"]
    excerpts = [c.content_excerpt for c in seen["candidates"]]
    # the context takes at most half the excerpt, then the chunk's own text follows
    assert excerpts == ["Lantern handboo\n\ntext of a.md", "text of b.md"]
    assert seen["endpoint"] == "builtin:openai" and seen["query"] == "refunds"


@pytest.mark.asyncio
@pytest.mark.parametrize("copy", [False, True], ids=["input-back", "same-order-copy"])
async def test_a_rerank_that_keeps_the_fused_order_says_none_ran(monkeypatch, copy) -> None:
    """The service hands its input back on a failure, and a reply it could not read
    comes back as a copy in input order: neither is a reranker's order."""

    async def _kept(**kw: Any) -> list[Any]:
        return list(kw["candidates"]) if copy else kw["candidates"]

    monkeypatch.setattr(rerank_mod, "rerank", _kept)
    hits = [_hit("a.md"), _hit("b.md")]
    out, reranked = await ks._rerank("builtin:openai", "q", hits, {}, 30)
    assert reranked is False and out is hits


def _stub_handler(
    monkeypatch, hits: list[Any], pools: list[int] | None = None, seen: list[str] | None = None
) -> None:
    """The handler over fixed fused `hits` in namespace "n", a reranker that reverses
    its candidates (their excerpts recorded in `seen`), and the legs' pool size recorded
    in `pools`."""
    seen = seen if seen is not None else []

    class _Embedder:
        model_name = "m"

        async def embed(self, text: str) -> list[float]:
            return [1.0]

    def _search(input: Any, embedder: Any, vec: Any, pool: int):
        if pools is not None:
            pools.append(pool)

        async def _run(namespaces: list[str]) -> list[tuple[str, list[Any]]]:
            return [("n", list(hits))]

        return _run

    async def _first(input: Any, namespaces: list[str], search: Any) -> Any:
        return await search(namespaces)

    async def _named(input: Any) -> list[str]:
        return ["n"]

    async def _reversed(**kw: Any) -> list[Any]:
        seen.extend(c.content_excerpt for c in kw["candidates"])
        return list(reversed(kw["candidates"]))

    monkeypatch.setattr(ks, "get_embedder", _Embedder)
    monkeypatch.setattr(ks, "_namespaces_search", _search)
    monkeypatch.setattr(ks, "_search_private_first", _first)
    monkeypatch.setattr(ks, "_named_namespaces", _named)
    monkeypatch.setattr(rerank_mod, "rerank", _reversed)


@pytest.mark.asyncio
async def test_the_search_reads_a_rerank_pool_and_reports_the_reranker(monkeypatch) -> None:
    """The legs fetch the rerank head even when the page is smaller, the reranked order
    is the page, and `reranked_by` names the reranker."""
    from server.knowledge.app.search import ChunkHit

    monkeypatch.setattr(settings, "knowledge_search_rerank_top", 4)
    pools: list[int] = []
    hits = [
        ChunkHit("n", f"{c}.md", 0, c, [], distance=0.1 * i, context=f"ctx {c}")
        for i, c in enumerate("abcd")
    ]
    seen: list[str] = []
    _stub_handler(monkeypatch, hits, pools, seen)
    out = await ks.handle_knowledge_search(
        _input(rerank="builtin:openai", top_k=2, namespaces=["n"], collapse_sections=False)
    )
    assert pools == [4]
    # each chunk's stored context reaches the reranker, joined by its namespace and key
    assert seen == ["ctx a\n\na", "ctx b\n\nb", "ctx c\n\nc", "ctx d\n\nd"]
    assert [h.source_uri for h in out.results] == ["d.md", "c.md"]
    assert out.reranked_by == "builtin:openai"
    plain = await ks.handle_knowledge_search(
        _input(top_k=2, namespaces=["n"], collapse_sections=False)
    )
    assert pools[-1] == 0 and plain.reranked_by == ""  # no reranker: no head to fetch
    assert [h.source_uri for h in plain.results] == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_each_chunk_shows_its_own_namespaces_context(monkeypatch) -> None:
    """Two namespaces hold the same source URI under different contexts: each chunk is
    shown its own namespace's context."""
    from server.knowledge.app.search import ChunkHit

    seen: list[str] = []
    _stub_handler(monkeypatch, [], seen=seen)
    per_ns = [
        (ns, [ChunkHit(ns, "a.md", 0, f"text {ns}", [], distance=d, context=f"ctx {ns}")])
        for ns, d in (("n", 0.1), ("m", 0.2))
    ]

    def _search(input: Any, embedder: Any, vec: Any, pool: int):
        async def _run(namespaces: list[str]) -> list[tuple[str, list[Any]]]:
            return per_ns

        return _run

    async def _named(input: Any) -> list[str]:
        return ["n", "m"]

    monkeypatch.setattr(ks, "_namespaces_search", _search)
    monkeypatch.setattr(ks, "_named_namespaces", _named)
    await ks.handle_knowledge_search(
        _input(rerank="builtin:openai", namespaces=["n", "m"], collapse_sections=False)
    )
    assert sorted(seen) == ["ctx m\n\ntext m", "ctx n\n\ntext n"]


@pytest.mark.asyncio
async def test_sections_collapse_after_the_rerank_so_its_pick_represents_the_section(
    monkeypatch,
) -> None:
    from server.knowledge.app.search import ChunkHit

    monkeypatch.setattr(settings, "knowledge_search_rerank_top", 4)
    hits = [
        ChunkHit("n", "a.md", i, f"part {i}", [], distance=0.1 * i, section_path=("S",))
        for i in range(4)
    ]
    _stub_handler(monkeypatch, hits)
    out = await ks.handle_knowledge_search(_input(rerank="builtin:openai", namespaces=["n"]))
    # the reranker's first chunk of the section survives and folds the other three
    assert [(h.chunk_idx, h.siblings_folded) for h in out.results] == [(3, 3)]


@pytest.mark.asyncio
async def test_a_held_model_call_fails_the_search_instead_of_keeping_the_fused_order(
    monkeypatch,
) -> None:
    from server.llm import customer

    async def _held(**kw: Any) -> list[Any]:
        raise customer.HOLDS[0]("held")

    from server.knowledge.app.search import ChunkHit

    hits = [ChunkHit("n", f"{c}.md", 0, c, [], distance=0.1) for c in "ab"]
    _stub_handler(monkeypatch, hits)
    monkeypatch.setattr(rerank_mod, "rerank", _held)
    with pytest.raises(customer.HOLDS):
        await ks.handle_knowledge_search(_input(rerank="builtin:openai", namespaces=["n"]))


@pytest.mark.asyncio
async def test_a_reranker_past_its_time_limit_keeps_the_fused_order(monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(settings, "knowledge_search_rerank_timeout_s", 0.01)

    async def _slow(**kw: Any) -> list[Any]:
        await asyncio.sleep(1)
        return list(reversed(kw["candidates"]))

    monkeypatch.setattr(rerank_mod, "rerank", _slow)
    hits = [_hit("a.md"), _hit("b.md")]
    out, reranked = await ks._rerank("builtin:openai", "q", hits, {}, 30)
    assert reranked is False and out is hits


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], "busy", {"results": "x"}], ids=["list", "str", "field"])
async def test_a_cohere_reply_without_a_results_list_keeps_the_input_order(
    monkeypatch, payload
) -> None:
    import httpx

    from server.core import provider_keys

    async def _post(url: str, body: Any, headers: Any) -> httpx.Response:
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(provider_keys, "rerank_key", lambda name: "key")
    monkeypatch.setattr(rerank_mod, "_post", _post)
    candidates = [rerank_mod.RerankCandidate(str(i), "", "v", "document", 1.0) for i in range(2)]
    out = await rerank_mod._rerank_with_builtin_cohere("builtin:cohere", "q", candidates)
    assert out is candidates
