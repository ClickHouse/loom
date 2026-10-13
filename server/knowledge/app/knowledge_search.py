"""knowledge.search: hybrid (vector + lexical + tags + recency) over one namespace or the
caller's namespaces, the addressed one and the private ones first.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from server.auth.fanout import namespaces_to_search
from server.embedding import get_embedder
from server.identity.app.namespace_visibility import private_first_passes
from server.knowledge.api.models_knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from server.embedding import Embedder
    from server.knowledge.app.search import ChunkHit


async def handle_knowledge_search(input: KnowledgeSearchInput) -> KnowledgeSearchOutput:
    from server.observability.otel import traced

    embedder = get_embedder()
    with traced("embed.compute.query", model=embedder.model_name):
        query_vec = await embedder.embed(input.query)
    search_one = _namespace_search(input, embedder, query_vec)
    namespaces = await namespaces_to_search(input.org, input.namespace, input.namespace_scope)
    results_per_ns = await _search_private_first(input, namespaces, search_one)
    all_hits = _as_search_hits(results_per_ns)
    all_hits.sort(key=lambda x: (x.score, x.similarity), reverse=True)
    if input.collapse_sections:
        all_hits = _collapse_by_section(all_hits)
    top = all_hits[: input.top_k]
    if input.narrow_spans and top:
        await _narrow_spans(embedder, query_vec, top)
    return KnowledgeSearchOutput(results=top, embedding_model=embedder.model_name)


def _namespace_search(
    input: KnowledgeSearchInput, embedder: Embedder, query_vec: list[float]
) -> Callable[[str], Awaitable[tuple[str, list[ChunkHit]]]]:
    """One namespace's search in the requested mode: the vector leg, the
    lexical leg, or both fused."""
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.knowledge.app.search import (
        HybridSearchQuery,
        fuse_rrf,
        hybrid_search,
        lexical_search,
    )

    tokens = knowledge_bm25.tokenize(input.query) if input.search_mode != "semantic" else []

    async def _search_one(ns: str) -> tuple[str, list[ChunkHit]]:
        base = HybridSearchQuery(
            org=input.org,
            namespace=ns,
            embedding_model=embedder.model_name,
            query_vec=query_vec,
            top_k=input.top_k,
            tags=list(input.tags or []),
            since_seconds=input.since_seconds,
            uri_prefixes=list(input.uri_prefix or []),
        )
        if input.search_mode == "semantic" or not tokens:
            return ns, await hybrid_search(base)
        # Lexical leg needs corpus statistics; a cold namespace (no view rows,
        # no avgdl) or a failed read degrades to the vector leg alone.
        from server.core.ch import get_client as _get_ch

        avgdl, idfs = await knowledge_bm25.lexical_stats(
            _get_ch(),
            org=input.org,
            namespace=ns,
            embedding_model=embedder.model_name,
            tokens=tokens,
        )
        lex_q = dataclasses.replace(base, tokens=tokens, idfs=idfs, avgdl=avgdl)
        if input.search_mode == "lexical":
            return ns, await lexical_search(lex_q)
        # Fusion candidates: fetch a wider slice from each leg so the top_k of
        # the fused list is drawn from more than top_k on either side.
        wide = dataclasses.replace(lex_q, top_k=max(input.top_k * 3, 20))
        sem, lex = await asyncio.gather(hybrid_search(wide), lexical_search(wide))
        if not lex:
            return ns, sem[: input.top_k]
        return ns, fuse_rrf(sem, lex, top_k=input.top_k, w_lex=input.lexical_weight)

    return _search_one


async def _search_private_first(
    input: KnowledgeSearchInput,
    namespaces: list[str],
    search_one: Callable[[str], Awaitable[tuple[str, list[ChunkHit]]]],
) -> list[tuple[str, list[ChunkHit]]]:
    from server.observability.otel import traced

    # Private-first (mirrors memory.search): under all_mine, the addressed
    # namespace and the private ones first; the public ones only if that pass
    # came up dry.
    PRIVATE_HIT_THRESHOLD = 0.30
    first_pass: list[str] = []
    second_pass: list[str] = []
    if input.namespace_scope == "all_mine" and len(namespaces) > 1:
        first_pass, second_pass = await private_first_passes(input.org, input.namespace, namespaces)
    if first_pass:
        with traced(
            "ch.query.chunks",
            op="vector_search.private",
            top_k=input.top_k,
            namespaces=len(first_pass),
        ):
            results_per_ns = await asyncio.gather(*[search_one(ns) for ns in first_pass])
        best_first_pass_similarity = 0.0
        for _ns, hits in results_per_ns:
            for h in hits:
                sim = 1.0 - getattr(h, "distance", 1.0)
                if sim > best_first_pass_similarity:
                    best_first_pass_similarity = sim
        if best_first_pass_similarity < PRIVATE_HIT_THRESHOLD and second_pass:
            with traced(
                "ch.query.chunks",
                op="vector_search.public",
                top_k=input.top_k,
                namespaces=len(second_pass),
            ):
                second_pass_results = await asyncio.gather(*[search_one(ns) for ns in second_pass])
            results_per_ns = list(results_per_ns) + list(second_pass_results)
    else:
        with traced(
            "ch.query.chunks",
            op="vector_search",
            top_k=input.top_k,
            namespaces=len(namespaces),
        ):
            results_per_ns = await asyncio.gather(*[search_one(ns) for ns in namespaces])
    return list(results_per_ns)


def _as_search_hits(results_per_ns: list[tuple[str, list[ChunkHit]]]) -> list[_SearchHit]:
    all_hits: list[_SearchHit] = []
    for ns, hits in results_per_ns:
        for h in hits:
            all_hits.append(
                _SearchHit(
                    source_uri=h.source_uri,
                    chunk_idx=h.chunk_idx,
                    content=h.content,
                    tags=h.tags,
                    distance=h.distance,
                    similarity=1.0 - h.distance,
                    namespace=ns,
                    section_path=list(h.section_path),
                    section=h.section,
                    lexical_score=h.lexical_score,
                    # semantic-only hits rank by similarity; fused / lexical
                    # hits carry the score their leg or the fusion assigned
                    score=h.score if h.score else (1.0 - h.distance),
                )
            )
    return all_hits


async def _narrow_spans(embedder: Embedder, query_vec: list[float], top: list[_SearchHit]) -> None:
    """The sentence window nearest the query, per hit, from one batched embed over every
    returned chunk's windows. Fail-open."""
    from server.observability.otel import traced

    try:
        from server.memory.app.spans import best_spans

        many = getattr(embedder, "embed_many", None)

        async def _embed_many(texts: list[str]) -> list[list[float]]:
            if many is not None:
                return list(await many(texts))
            return [await embedder.embed(t) for t in texts]

        with traced("embed.compute.spans", n=len(top)):
            spans = await best_spans(query_vec, [h.content for h in top], _embed_many)
        for hit, sp in zip(top, spans, strict=False):
            if sp is not None:
                hit.span, hit.span_start, hit.span_end = sp.text, sp.start, sp.end
                hit.span_similarity = sp.similarity
    except Exception as e:  # noqa: BLE001 — the chunk is still the answer
        log.debug("knowledge.search span narrowing skipped: %s", e)


def _collapse_by_section(hits: list[_SearchHit]) -> list[_SearchHit]:
    """One hit per (namespace, document, section), best-ranked first, with the
    number of folded siblings on the survivor. Hits with no section path are
    left alone: a headless document's windows are distinct units."""
    seen: dict[tuple[str, str, tuple[str, ...]], _SearchHit] = {}
    out: list[_SearchHit] = []
    for h in hits:
        if not h.section_path:
            out.append(h)
            continue
        key = (h.namespace, h.source_uri, tuple(h.section_path))
        survivor = seen.get(key)
        if survivor is None:
            seen[key] = h
            out.append(h)
        else:
            survivor.siblings_folded += 1
    return out
