"""knowledge.search: hybrid (vector + lexical + tags + recency) over one namespace or the
caller's namespaces, the addressed one and the private ones first.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
from typing import TYPE_CHECKING

from server.auth.fanout import namespaces_to_search
from server.core.vectors.private_first import search_private_first
from server.embedding import get_embedder
from server.identity.app import namespace_visibility
from server.knowledge.api.models_knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)
from server.knowledge.domain.chunking import embedding_text
from server.memory.app.log_once import LogOnce
from server.observability import degradations as _degradations
from server.settings import settings

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from server.embedding import Embedder
    from server.knowledge.app.search import ChunkHit

# A first-pass hit this close (cosine similarity) ends a private-first search
# before its public pass.
PRIVATE_HIT_THRESHOLD = 0.30

# A rejected deployment default is logged once per value, not once per search.
_BAD_DEFAULT_LOGGED = LogOnce()


async def handle_knowledge_search(input: KnowledgeSearchInput) -> KnowledgeSearchOutput:
    from server.auth.context import current_auth
    from server.auth.gateway import is_end_user
    from server.observability.otel import traced

    if is_end_user(current_auth()) and "namespace_scope" not in input.model_fields_set:
        # An end user's search reads what it names (`namespaces`), not every
        # collection the user reaches: another agent's files stay out of it.
        input = input.model_copy(update={"namespace_scope": "this"})

    embedder = get_embedder()
    with traced("embed.compute.query", model=embedder.model_name):
        query_vec = await embedder.embed(input.query)
    endpoint = _rerank_endpoint(input)
    pool = settings.knowledge_search_rerank_top if endpoint else 0
    search = _namespaces_search(input, embedder, query_vec, pool)
    skipped: list[str] = []
    if input.namespaces:
        namespaces = await _named_namespaces(input)
    else:
        namespaces, skipped = await namespaces_to_search(
            input.org,
            input.namespace,
            input.namespace_scope,
            cap=settings.knowledge_search_max_namespaces,
            kinds=settings.knowledge_search_fanout_kinds,
        )
    results_per_ns = await _search_private_first(input, namespaces, search)
    all_hits = _as_search_hits(results_per_ns)
    all_hits.sort(key=lambda x: (x.score, x.similarity), reverse=True)
    reranked_by = ""
    if endpoint and len(all_hits) > 1:
        contexts = {
            (ns, h.source_uri, h.chunk_idx): h.context for ns, hits in results_per_ns for h in hits
        }
        all_hits, reranked = await _rerank(endpoint, input.query, all_hits, contexts, pool)
        reranked_by = endpoint if reranked else ""
    if input.collapse_sections:
        all_hits = _collapse_by_section(all_hits)
    top = all_hits[: input.top_k]
    if input.narrow_spans and top:
        await _narrow_spans(embedder, query_vec, top)
    return KnowledgeSearchOutput(
        results=top,
        embedding_model=embedder.model_name,
        namespaces_skipped=skipped,
        reranked_by=reranked_by,
    )


def _rerank_endpoint(input: KnowledgeSearchInput) -> str:
    """The reranker this call uses: the request's, else the deployment default; '' for
    none ("none" turns the default off). An LLM-free read reranks only as its request
    names, and never with the builtin LLM reranker; a default that is not a reranker a
    request may name (a URL, an unconfigured name, Cohere behind a gateway) is none."""
    from server.core.llm_free import reads_llm_free
    from server.memory.domain.rerank_names import BUILTIN_OPENAI, check_rerank_name

    if input.rerank == "none":
        return ""
    endpoint = input.rerank
    if not endpoint and not reads_llm_free():
        try:
            endpoint = check_rerank_name(settings.knowledge_search_rerank_default)
        except ValueError as e:
            if _BAD_DEFAULT_LOGGED.first(settings.knowledge_search_rerank_default):
                log.warning("knowledge_search_rerank_default is not used: %s", e)
            endpoint = ""
    if endpoint == "none" or (endpoint.startswith(BUILTIN_OPENAI) and reads_llm_free()):
        return ""
    return endpoint


async def _rerank(
    endpoint: str,
    query: str,
    hits: list[_SearchHit],
    contexts: dict[tuple[str, str, int], str],
    top: int,
) -> tuple[list[_SearchHit], bool]:
    """The fused head (`top` hits, the pool the legs fetched) in the reranker's order, the
    rest after it, and whether the reranker produced the order. Each candidate shows the
    chunk's context (title, description, breadcrumb) before its text. A held or refused
    model call fails the search; on a timeout, or any other failure (the rerank service
    hands back its input), the fused order is kept."""
    from server.memory.app.rerank import RerankCandidate, rerank

    chars = settings.knowledge_search_rerank_excerpt_chars
    head, tail = hits[:top], hits[top:]
    candidates = []
    for i, h in enumerate(head):
        # At most half the excerpt is context: a long description must not crowd out
        # the chunk's own text, or every chunk of a document reads the same.
        context = contexts.get((h.namespace, h.source_uri, h.chunk_idx), "")[: chars // 2]
        text = embedding_text(context, h.content)
        candidates.append(
            RerankCandidate(
                memory_key=str(i),
                namespace="",
                content_excerpt=text[:chars],
                memory_type="document",
                similarity=h.similarity,
            )
        )
    try:
        # A slow provider costs the page its rerank, never the search its answer.
        async with asyncio.timeout(settings.knowledge_search_rerank_timeout_s):
            ordered = await rerank(
                endpoint=endpoint,
                prompt_template="",
                query=query,
                candidates=candidates,
                excerpt_chars=chars,
            )
    except TimeoutError as e:
        _degradations.note("rerank:knowledge", "fused_order_kept", e)
        return hits, False
    # The service hands back its input on a failure, or an order it could not read in
    # input order: either way no reranker ordered the page.
    if [c.memory_key for c in ordered] == [c.memory_key for c in candidates]:
        return hits, False
    return [head[int(c.memory_key)] for c in ordered] + tail, True


def _namespaces_search(
    input: KnowledgeSearchInput, embedder: Embedder, query_vec: list[float], pool: int
) -> Callable[[list[str]], Awaitable[list[tuple[str, list[ChunkHit]]]]]:
    """A search over several namespaces in the requested mode, one statement
    per leg whatever their number: the vector leg, the lexical leg, or both
    fused. The hits come back grouped per namespace."""
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.knowledge.app.search import (
        HybridSearchQuery,
        fuse_rrf,
        hybrid_search,
        lexical_search,
    )

    tokens = knowledge_bm25.tokenize(input.query) if input.search_mode != "semantic" else []

    # Collapsing sections folds sibling chunks after the statement: ask for more,
    # so the page still fills. `pool` is a reranker's head when one applies (0 when
    # none does): the legs fetch at least that many, not that many times three.
    top_k = max(input.top_k * 3 if input.collapse_sections else input.top_k, pool)

    async def _ranked(namespaces: list[str]) -> list[ChunkHit]:
        base = HybridSearchQuery(
            org=input.org,
            namespaces=tuple(namespaces),
            embedding_model=embedder.model_name,
            query_vec=query_vec,
            top_k=top_k,
            tags=list(input.tags or []),
            since_seconds=input.since_seconds,
            uri_prefixes=list(input.uri_prefix or []),
            session_ids=list(input.session_ids),
            source_classes=list(input.source_classes),
        )
        if input.search_mode == "semantic" or not tokens:
            return await hybrid_search(base)
        # Lexical leg needs corpus statistics; a cold corpus (no view rows,
        # no avgdl) or a failed read degrades to the vector leg alone.
        from server.core.ch import get_client as _get_ch

        avgdl, idfs = await knowledge_bm25.lexical_stats(
            _get_ch(),
            org=input.org,
            namespaces=namespaces,
            embedding_model=embedder.model_name,
            tokens=tokens,
        )
        lex_q = dataclasses.replace(base, tokens=tokens, idfs=idfs, avgdl=avgdl)
        if input.search_mode == "lexical":
            return await lexical_search(lex_q)
        # Fusion candidates: fetch a wider slice from each leg so the top_k of
        # the fused list is drawn from more than top_k on either side.
        wide = dataclasses.replace(lex_q, top_k=max(top_k * 3, 20))
        sem, lex = await asyncio.gather(hybrid_search(wide), lexical_search(wide))
        if not lex:
            return sem[:top_k]
        return fuse_rrf(sem, lex, top_k=top_k, w_lex=input.lexical_weight)

    async def _search(namespaces: list[str]) -> list[tuple[str, list[ChunkHit]]]:
        by_ns: dict[str, list[ChunkHit]] = {}
        for hit in await _ranked(namespaces):
            by_ns.setdefault(hit.namespace, []).append(hit)
        return list(by_ns.items())

    return _search


async def _search_private_first(
    input: KnowledgeSearchInput,
    namespaces: list[str],
    search: Callable[[list[str]], Awaitable[list[tuple[str, list[ChunkHit]]]]],
) -> list[tuple[str, list[ChunkHit]]]:
    from server.observability.otel import traced

    return await search_private_first(
        input.namespace,
        namespaces,
        search,
        visibility=(
            functools.partial(namespace_visibility.visibility_map, input.org)
            if input.namespace_scope == "all_mine"
            else None
        ),
        threshold=PRIVATE_HIT_THRESHOLD,
        span=functools.partial(traced, "ch.query.chunks", top_k=input.top_k),
    )


async def _named_namespaces(input: KnowledgeSearchInput) -> list[str]:
    """`namespace` (the transport gated it) and the ones the request names,
    each gated as a read of its own (`@me` is the end user's personal
    collection): AuthInsufficientError for the first the caller may not read."""
    from server.auth.authz import check_authorized
    from server.auth.context import current_auth
    from server.auth.gateway import personal_namespace
    from server.core.errors import AuthInsufficientError, ValidationError
    from server.operations.base import Role

    auth = current_auth()
    out = [input.namespace]
    for name in input.namespaces:
        try:
            namespace = personal_namespace(auth, input.org, name)
        except ValueError as e:
            raise ValidationError(str(e)) from e
        if namespace in out:
            continue
        try:
            await check_authorized(auth, Role.VIEWER, org=input.org, namespace=namespace)
        except PermissionError as e:
            raise AuthInsufficientError(str(e)) from e
        out.append(namespace)
    return out


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
                    ingested_at=h.ingested_at,
                    section_path=list(h.section_path),
                    section=h.section,
                    lexical_score=h.lexical_score,
                    # semantic-only hits rank by similarity; fused / lexical
                    # hits carry the score their leg or the fusion assigned
                    score=h.score if h.score else (1.0 - h.distance),
                    agent=h.agent,
                    session_id=h.session_id,
                    source_class=h.source_class,
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
