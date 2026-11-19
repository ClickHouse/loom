"""knowledge.search over one namespace or the caller's namespaces, the addressed one
and the private ones first: in hybrid mode the vector, BM25 and exact-key legs fused
by reciprocal rank, each under the request's filters.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from server.auth.fanout import namespaces_to_search
from server.core.diagnostics import private_work
from server.core.vectors.private_first import search_private_first
from server.embedding import get_embedder
from server.identity.app import namespace_visibility
from server.knowledge.api.models_knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)
from server.knowledge.app import gaps as gaps_app
from server.knowledge.app import source_publication
from server.knowledge.app.readable import readable_namespaces
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.app.source_search_proofs import SearchProofs
from server.knowledge.domain.chunking import embedding_text
from server.memory.app.log_once import LogOnce
from server.settings import settings

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from server.embedding import Embedder
    from server.knowledge.app.search import ChunkHit
    from server.knowledge.app.search_legs import LegPools
    from server.knowledge.app.source_ranking import RankedHit
    from server.knowledge.app.source_search_passes import SearchPasses

# A hit this close (cosine similarity) answers the query: as a first-pass hit it ends a
# private-first search before its public pass, and it ranks ahead of noise sections.
PRIVATE_HIT_THRESHOLD = 0.30

# A rejected deployment default is logged once per value, not once per search.
_BAD_DEFAULT_LOGGED = LogOnce()


@dataclasses.dataclass(frozen=True)
class _SearchRenderer:
    input: KnowledgeSearchInput
    embedder: Embedder
    query_vec: list[float]
    endpoint: str
    pool: int
    skipped: list[str]
    marked: bool

    async def render(
        self,
        rows: list[tuple[str, list[ChunkHit]]],
        dropped: int,
        proofs: SearchProofs | None = None,
    ) -> KnowledgeSearchOutput:
        with private_work() if self.marked else nullcontext():
            return await self._render(rows, dropped, proofs)

    async def _render(
        self,
        rows: list[tuple[str, list[ChunkHit]]],
        dropped: int,
        proofs: SearchProofs | None,
    ) -> KnowledgeSearchOutput:
        all_hits = _as_search_hits(rows)
        all_hits.sort(key=lambda hit: (hit.score, hit.similarity), reverse=True)
        if proofs is not None:
            await proofs.check()
        reranked_by = ""
        if self.endpoint and len(all_hits) > 1:
            all_hits, reranked = await _rerank(self.endpoint, self.input.query, all_hits, self.pool)
            if proofs is not None:
                await proofs.check()
            reranked_by = self.endpoint if reranked else ""
        if self.input.collapse_sections:
            all_hits = _collapse_by_section(all_hits)
        top = noise_last(all_hits)[: self.input.top_k]
        if self.input.narrow_spans and top:
            await _narrow_spans(self.embedder, self.query_vec, top, proofs=proofs)
        if proofs is not None:
            await proofs.check()
        if _missed(self.input, top, skipped=bool(self.skipped), dropped=bool(dropped)):
            gaps_app.record_miss(
                op="knowledge.search",
                org=self.input.org,
                namespace=self.input.namespace,
                query=self.input.query,
            )
        return KnowledgeSearchOutput(
            results=top,
            embedding_model=self.embedder.model_name,
            namespaces_skipped=self.skipped,
            reranked_by=reranked_by,
            stale_dropped=dropped,
        )


@dataclasses.dataclass
class CheckedSearch:
    output: KnowledgeSearchOutput
    proofs: SearchProofs | None
    _renderer: _SearchRenderer
    _ordinary: list[tuple[str, list[ChunkHit]]]
    _ordinary_dropped: int
    lost: bool = False
    ordinary_rerank_possible: bool = False
    _failed: bool = dataclasses.field(default=False, init=False)
    _checking: asyncio.Lock = dataclasses.field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    @property
    def marked(self) -> bool:
        return self._renderer.marked

    @property
    def metadata_uses_mirrors(self) -> bool:
        """Private routing can change retained metadata after all its hits are displaced."""
        return self.proofs is not None and (
            bool(self.output.reranked_by)
            or self.ordinary_rerank_possible
            or self.output.stale_dropped != self._ordinary_dropped
        )

    async def _fallback(self) -> KnowledgeSearchOutput:
        if self._failed:
            raise LostCorpus()
        if self.proofs is not None:
            source_publication.discard(self.proofs)
            self.proofs, self.lost = None, True
            try:
                self.output = await self._renderer.render(self._ordinary, self._ordinary_dropped)
            except BaseException:
                self._failed = True
                raise
        return self.output

    async def ordinary(self) -> KnowledgeSearchOutput:
        async with self._checking:
            with private_work() if self.marked else nullcontext():
                return await self._fallback()

    async def current(self) -> KnowledgeSearchOutput:
        """Return the renewed reply, rebuilding one ordinary fallback after access loss."""
        async with self._checking:
            with private_work() if self.marked else nullcontext():
                if self._failed:
                    raise LostCorpus()
                if self.proofs is not None:
                    try:
                        await self.proofs.check()
                    except LostCorpus:
                        return await self._fallback()
                return self.output


@dataclasses.dataclass(frozen=True)
class SearchRequest:
    input: KnowledgeSearchInput
    namespaces: tuple[str, ...]
    skipped: tuple[str, ...]


async def handle_knowledge_search(input: KnowledgeSearchInput) -> KnowledgeSearchOutput:
    checked = await checked_knowledge_search(input)
    source_publication.retain(checked.proofs)
    return checked.output


async def resolve_search(input: KnowledgeSearchInput) -> SearchRequest:
    from server.auth.context import current_auth
    from server.auth.gateway import is_end_user

    input = input.model_copy(deep=True)
    if is_end_user(current_auth()) and "namespace_scope" not in input.model_fields_set:
        # An end user's search reads what it names (`namespaces`), not every
        # collection the user reaches: another agent's files stay out of it.
        input = input.model_copy(update={"namespace_scope": "this"})

    skipped: list[str] = []
    if input.namespaces:
        namespaces = await readable_namespaces(input.org, input.namespace, input.namespaces)
    else:
        namespaces, skipped = await namespaces_to_search(
            input.org,
            input.namespace,
            input.namespace_scope,
            cap=settings.knowledge_search_max_namespaces,
            kinds=settings.knowledge_search_fanout_kinds,
        )
    return SearchRequest(input, tuple(namespaces), tuple(skipped))


async def checked_knowledge_search(input: KnowledgeSearchInput) -> CheckedSearch:
    from server.knowledge.app.source_access import prepare

    request = await resolve_search(input)
    plan = await prepare(request.input.org, list(request.namespaces))
    return await run_checked_search(request, plan)


async def run_checked_search(request: SearchRequest, plan: ReadPlan) -> CheckedSearch:
    if request.input.org != plan.org:
        raise ValueError("document read scope changed")
    with private_work() if plan.marked else nullcontext():
        return await _run_search(request, plan)


async def _run_search(request: SearchRequest, plan: ReadPlan) -> CheckedSearch:
    from server.knowledge.app.source_search_passes import SearchPasses
    from server.observability.otel import traced

    input = request.input
    namespaces, skipped = list(request.namespaces), list(request.skipped)
    checked = SearchPasses(plan) if plan.marked else None
    embedder = get_embedder()
    with traced("embed.compute.query", model=embedder.model_name):
        query_vec = await embedder.embed(input.query)
    endpoint = _rerank_endpoint(input)
    pool = settings.knowledge_search_rerank_top if endpoint else 0
    dropped = _Dropped()
    search = _namespaces_search(input, embedder, query_vec, pool, dropped, checked=checked)
    if checked is not None:
        results_per_ns, ordinary, ordinary_dropped = await _search_marked(
            input, namespaces, search, checked, dropped
        )
        proofs: SearchProofs | None = checked.proofs if checked.proofs.mirrored else None
    else:
        results_per_ns = await _search_private_first(input, namespaces, search)
        ordinary, ordinary_dropped, proofs = results_per_ns, dropped.count, None
    renderer = _SearchRenderer(input, embedder, query_vec, endpoint, pool, skipped, plan.marked)
    lost = False
    try:
        output = await renderer.render(results_per_ns, dropped.count, proofs)
    except LostCorpus:
        output = await renderer.render(ordinary, ordinary_dropped)
        proofs, lost = None, True
    return CheckedSearch(
        output,
        proofs,
        renderer,
        ordinary,
        ordinary_dropped,
        lost=lost,
        ordinary_rerank_possible=bool(endpoint) and sum(len(hits) for _, hits in ordinary) > 1,
    )


# The fields that narrow a knowledge.search to part of the namespace's documents.
_HARD_FILTERS = (
    "tags",
    "since_seconds",
    "namespaces",
    "uri_prefix",
    "session_ids",
    "source_classes",
    "formats",
    "block_types",
)


def _missed(
    input: KnowledgeSearchInput, top: list[_SearchHit], *, skipped: bool, dropped: bool
) -> bool:
    """No hit answers an unfiltered query: none as close as the noise filter's threshold,
    none an exact-key match, and in lexical mode none at all (its hits are BM25's,
    whatever their cosine). A fan-out that stopped short, or hits the authority dropped,
    is no gap."""
    if not settings.knowledge_gaps_enabled or skipped or dropped:
        return False
    if gaps_app.filtered(input, _HARD_FILTERS):
        return False
    if input.search_mode == "lexical" and top:
        return False
    return not any(h.similarity >= _close() or h.keys_matched for h in top)


def noise_last(hits: list[_SearchHit]) -> list[_SearchHit]:
    """`hits` with the noise sections moved below the last hit close to the query
    (`_close`): every hit ranked above that one, close or not, comes before them, and
    every other hit keeps its order. A query about the noise itself still finds it when
    no close hit ranks below it. As given when `knowledge_noise_filter` is off."""
    if not settings.knowledge_noise_filter:
        return hits
    close = _close()
    last = max((i for i, h in enumerate(hits) if not h.noise and h.similarity >= close), default=-1)
    head = hits[: last + 1]
    return [h for h in head if not h.noise] + [h for h in head if h.noise] + hits[last + 1 :]


def not_noise() -> Callable[[Any], bool] | None:
    """The predicate that drops noise sections from what an agent is handed unasked
    (context.pack, memory.stack); None, keeping every hit, with the filter off."""
    return (lambda h: not h.noise) if settings.knowledge_noise_filter else None


def _close() -> float:
    """The similarity of a hit that answers the query: `PRIVATE_HIT_THRESHOLD`, or the
    embedder's calibrated noise floor where unrelated text scores higher."""
    from server.memory.app.embedder_calibration import effective_noise_floor

    return max(PRIVATE_HIT_THRESHOLD, effective_noise_floor(PRIVATE_HIT_THRESHOLD))


def _rerank_endpoint(input: KnowledgeSearchInput) -> str:
    """The reranker this call uses: the request's, else the deployment default; '' for
    none ("none" turns the default off). An LLM-free read reranks only as its request
    names, and never with the builtin LLM reranker; a default that is not a reranker a
    request may name (a URL, an unconfigured name, Cohere behind a gateway) is none."""
    from server.core.llm_free import reads_llm_free
    from server.memory.domain.rerank_names import BUILTIN_OPENAI, check_rerank_name

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
    top: int,
) -> tuple[list[_SearchHit], bool]:
    """The first `top` fused hits in the reranker's order, the rest after it, and
    whether the reranker produced the order. Each candidate shows the chunk's context
    (title, description, breadcrumb) before its text. A held or refused model call fails
    the search; on a timeout, or any other failure (the rerank service hands back its
    input), the fused order is kept."""
    from server.memory.app.rerank import RerankCandidate, rerank

    chars = settings.knowledge_search_rerank_excerpt_chars
    head, tail = hits[:top], hits[top:]
    candidates = []
    for i, h in enumerate(head):
        # At most half the excerpt is context: a long description must not crowd out
        # the chunk's own text, or every chunk of a document reads the same.
        context = h._context[: chars // 2]
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
    except TimeoutError:
        # The search's own deadline, not the provider's failure: no health record.
        log.info("knowledge.search rerank cut at %ss", settings.knowledge_search_rerank_timeout_s)
        return hits, False
    # The service hands back its input on a failure, or an order it could not read in
    # input order: either way no reranker ordered the page.
    if [c.memory_key for c in ordered] == [c.memory_key for c in candidates]:
        return hits, False
    return [head[int(c.memory_key)] for c in ordered] + tail, True


@dataclasses.dataclass
class _Dropped:
    """The candidates a search dropped as not served (`pg_serving`)."""

    count: int = 0


def _namespaces_search(
    input: KnowledgeSearchInput,
    embedder: Embedder,
    query_vec: list[float],
    pool: int,
    dropped: _Dropped,
    *,
    checked: SearchPasses | None = None,
) -> Callable[[list[str]], Awaitable[list[tuple[str, list[ChunkHit]]]]]:
    """A search over several namespaces in the requested mode, one statement per leg
    whatever their number: the vector leg alone (semantic), the lexical leg alone
    (lexical), or in hybrid mode the vector, lexical and exact-key legs fused. The hits
    come back grouped per namespace, each one its source's head serves
    (`pg_serving.served_search`), and what is left out is counted in `dropped`."""
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.knowledge.app import pg_serving
    from server.knowledge.app.search import HybridSearchQuery
    from server.knowledge.app.search_legs import collect
    from server.knowledge.domain.exact_keys import query_keys

    tokens = knowledge_bm25.tokenize(input.query) if input.search_mode != "semantic" else []
    # The exact-key leg runs in hybrid mode, fused with the other two.
    keys = query_keys(input.query) if input.search_mode == "hybrid" else []

    # Collapsing sections folds sibling chunks after the statement: ask for more,
    # so the page still fills. `pool` is a reranker's head when one applies (0 when
    # none does): the legs fetch at least that many, not that many times three.
    page_k = max(input.top_k * 3 if input.collapse_sections else input.top_k, pool)

    async def _ranked(namespaces: list[str], top_k: int) -> LegPools:
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
            formats=list(input.formats),
            block_types=list(input.block_types),
            tokens=tokens,
            keys=keys,
        )
        return await collect(base, input.search_mode, lexical_weight=input.lexical_weight)

    async def _search(namespaces: list[str]) -> list[tuple[str, list[ChunkHit]]]:
        captured: list[LegPools] = []

        async def fetch(top_k: int) -> list[ChunkHit]:
            legs = await _ranked(namespaces, top_k)
            captured[:] = [legs]
            return legs.ranked()

        hits, gone = await pg_serving.served_search(input.org, fetch, page_k)
        dropped.count += gone
        if checked is not None:
            legs = captured[0]
            serving, fallback = await legs.serving(hits)
            dropped.count += len(hits) - len(fallback)
            hits = fallback
            await checked.collect(
                legs.query,
                serving,
                legs=tuple(legs.pools),
                fallback=hits,
                top_k=page_k,
                lexical_weight=input.lexical_weight,
            )
        by_ns: dict[str, list[ChunkHit]] = {}
        for hit in hits:
            by_ns.setdefault(hit.namespace, []).append(hit)
        return list(by_ns.items())

    return _search


async def _search_marked(
    input: KnowledgeSearchInput,
    namespaces: list[str],
    search: Callable[[list[str]], Awaitable[list[tuple[str, list[ChunkHit]]]]],
    checked: SearchPasses,
    dropped: _Dropped,
) -> tuple[list[tuple[str, list[ChunkHit]]], list[tuple[str, list[ChunkHit]]], int]:
    from server.core.ch import set_query_tag
    from server.core.vectors.private_first import best_similarity, private_first_passes

    possible = [namespaces]
    if input.namespace_scope == "all_mine" and len(namespaces) > 1:
        visibility = await namespace_visibility.visibility_map(input.org, namespaces)
        split = private_first_passes(input.namespace, namespaces, visibility)
        if split is not None:
            possible = [names for names in split if names]
    counts = []
    set_query_tag("knowledge.mirror.search_passes")
    for names in possible:
        before = dropped.count
        await search(names)
        counts.append(dropped.count - before)

    def selected(
        passes: list[list[RankedHit]],
    ) -> tuple[list[tuple[str, list[ChunkHit]]], int, int]:
        if len(passes) > 1:
            first = [(item.chunk.namespace, [item.chunk]) for item in passes[0]]
            if best_similarity(first, not_noise()) >= PRIVATE_HIT_THRESHOLD:
                passes = passes[:1]
        by_ns: dict[str, list[ChunkHit]] = {}
        for pool in passes:
            for item in pool:
                by_ns.setdefault(item.chunk.namespace, []).append(item.chunk)
        return list(by_ns.items()), sum(counts[: len(passes)]), len(passes)

    finished = await checked.finish()
    final, dropped.count, used = selected(finished)
    checked.restrict_proofs(finished[:used])
    ordinary, ordinary_dropped, _ = selected(checked.ordinary())
    return final, ordinary, ordinary_dropped


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
        # A noise section near the query does not stand in for the public documents.
        may_end=not_noise(),
        span=functools.partial(traced, "ch.query.chunks", top_k=input.top_k),
    )


def _as_search_hits(results_per_ns: list[tuple[str, list[ChunkHit]]]) -> list[_SearchHit]:
    all_hits: list[_SearchHit] = []
    for ns, hits in results_per_ns:
        for h in hits:
            hit = _SearchHit(
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
                revision=h.revision,
                format=h.format,
                block_type=h.block_type,
                locator=h.locator,
                keys_matched=list(h.keys_matched),
                noise=h.noise,
            )
            hit._context = h.context  # per hit: two versions of one chunk carry their own
            all_hits.append(hit)
    return all_hits


async def _narrow_spans(
    embedder: Embedder,
    query_vec: list[float],
    top: list[_SearchHit],
    *,
    proofs: SearchProofs | None = None,
) -> None:
    """The sentence window nearest the query, per hit, from one batched embed over every
    returned chunk's windows. Fail-open."""
    from server.observability.otel import traced

    try:
        from server.memory.app.spans import best_spans

        many = getattr(embedder, "embed_many", None)

        async def _embed_one(text: str) -> list[float]:
            if proofs is not None:
                await proofs.check()
            vector = await embedder.embed(text)
            if proofs is not None:
                await proofs.check()
            return vector

        async def _embed_many(texts: list[str]) -> list[list[float]]:
            if many is not None:
                if proofs is not None:
                    await proofs.check()
                vectors = list(await many(texts))
                if proofs is not None:
                    await proofs.check()
                return vectors
            return [await _embed_one(text) for text in texts]

        with traced("embed.compute.spans", n=len(top)):
            spans = await best_spans(query_vec, [h.content for h in top], _embed_many)
        for hit, sp in zip(top, spans, strict=False):
            if sp is not None:
                hit.span, hit.span_start, hit.span_end = sp.text, sp.start, sp.end
                hit.span_similarity = sp.similarity
    except LostCorpus:
        raise
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
