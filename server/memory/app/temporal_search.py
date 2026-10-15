"""memory.search at (V, K): the eligible assertion set is fixed first, then ranked.

Eligibility is exact — the assertions served at each namespace's cutoff that
apply at V, with the request's type and tag filters applied after the set is
reduced — so no ANN limit can drop a match and a retired assertion never comes
back because its replacement fails a filter. Ranking is an exact cosine over the
historical vector projection; an assertion whose vector is still pending ranks
lexically and the cutoff says how many did. Ranking signals are today's
(`ranking_current`): the contents are historical, the scoring is not.
"""

from __future__ import annotations

import asyncio
import re

from server.auth.fanout import namespaces_to_search, resolve_fanout
from server.core.errors import ValidationError
from server.embedding import get_embedder
from server.identity.app.request_scope import request_scope
from server.memory.api.models_common import TemporalCutoff
from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.memory.app import temporal_reads as reads
from server.memory.app.embedder_calibration import (
    effective_abstain_threshold,
    effective_noise_floor,
    resolve_calibration,
)
from server.memory.app.search.payload import _apply_bodies
from server.memory.domain.excerpt import build_excerpt
from server.memory.domain.fact_quality import drop_junk_and_restatements
from server.memory.domain.memory_types import HIDDEN_FROM_UNFILTERED_SEARCH, is_searchable_type
from server.memory.domain.temporal_reads import Axes, to_epoch_ns
from server.memory.domain.trust import attests
from server.memory.store import temporal as store
from server.settings import settings

_WORD = re.compile(r"\w+")


def _unsupported(input: MemorySearchInput) -> list[str]:
    """memory.search filters the journal cannot answer: it records no run id,
    entity match or category."""
    out = []
    if input.run_id not in (None, "", "-", "*"):
        out.append("run_id")
    if input.entity_filter:
        out.append("entity_filter")
    if input.category and not input.category_soft:
        out.append("category")
    if input.bodies in ("unit", "source"):
        # Whole values within a budget, or a linked source, are read from today's rows.
        out.append(f"bodies={input.bodies!r}")
    return out


async def search(input: MemorySearchInput) -> MemorySearchOutput:
    unsupported = _unsupported(input)
    if unsupported:
        raise ValidationError(f"a historical memory.search does not take {', '.join(unsupported)}")
    axes = reads.axes_of(input)
    namespaces, skipped = await namespaces_to_search(
        input.org, input.namespace, input.namespace_scope
    )
    embedder = get_embedder()
    query_vec = await embedder.embed(input.query)
    if len(namespaces) > 1 and (await resolve_fanout(input.org)).ordered:
        # A scoped token's order is a priority, read as a current search reads it:
        # one namespace at a time, stopping at the first with a confident hit.
        per_ns = []
        for ns in namespaces:
            per_ns.append(await _namespace_hits(input, ns, axes, embedder.model_name, query_vec))
            best = max((h.similarity for h in per_ns[-1][0]), default=0.0)
            if best >= settings.memory_search_tier_stop_similarity:
                break
    else:
        per_ns = list(
            await asyncio.gather(
                *(
                    _namespace_hits(input, ns, axes, embedder.model_name, query_vec)
                    for ns in namespaces
                )
            )
        )
    # The same gates as a current search: the calibrated noise floor drops a row, and
    # a best row under the abstain threshold says "I don't know".
    cal = await resolve_calibration(input.org, input.namespace)
    floor = max(
        effective_noise_floor(settings.memory_search_min_similarity, cal),
        input.min_similarity or 0.0,
    )
    hits = [h for got, _ in per_ns for h in got if h.similarity >= floor]
    hits.sort(key=lambda h: (-h.similarity, h.memory_key))
    if input.quality_filter:
        hits, _ = drop_junk_and_restatements(hits)
    hits = hits[: input.top_k]
    _apply_bodies(hits, input)
    abstain = not hits or hits[0].similarity < effective_abstain_threshold(
        settings.memory_search_abstain_threshold, cal
    )
    _record_exposure(input, hits, [cut for _, cut in per_ns])
    return MemorySearchOutput(
        results=hits,
        namespaces_searched=namespaces,
        namespaces_skipped=skipped,
        candidates_total=sum(len(got) for got, _ in per_ns),
        embedding_model=embedder.model_name,
        abstain=abstain,
        retrieval_risk="abstain" if abstain else "low",
        temporal=[cut for _, cut in per_ns],
    )


async def _namespace_hits(
    input: MemorySearchInput, ns: str, axes: Axes, model: str, query_vec: list[float]
) -> tuple[list[_MemorySearchHit], TemporalCutoff]:
    if input.scope == "shared":
        return await _shared_hits(input, ns, axes)
    cut = await reads.cutoff(
        input.org, ns, axes, read_token=input.read_token, policy=input.validity_policy
    )
    rows = await reads.served(
        input.org,
        cut,
        scope=request_scope(),
        agent=None if input.agent == "*" else input.agent,
        user_id=None if input.user_id in (None, "", "*") else input.user_id,
        session_id=None if input.session_id in (None, "", "*") else input.session_id,
        memory_type=input.memory_type,
        tags=() if input.tags_soft else input.tags,
    )
    include = reads.statuses(
        include_proposed=input.include_proposed, include_duplicates=input.include_duplicates
    )
    held = [h for hs, _ in reads.held_at(rows, cut, include=include).values() for h in hs]
    held = [h for h in held if _wanted(h, input)]
    shas = sorted({h.a.embed_input_sha256 for h in held})
    dist = await store.rank_by_vector(
        input.org, ns, model, query_vec, shas, limit=max(len(shas), 1)
    )
    pending = sum(1 for h in held if h.a.embed_input_sha256 not in dist)
    wire = cut.wire()
    wire.gaps.append("ranking_current")
    if pending:
        wire.gaps.append(f"vectors_pending:{pending}")
        wire.complete = False
    terms = set(_WORD.findall(input.query.lower()))
    out = []
    for h in held:
        d = dist.get(h.a.embed_input_sha256)
        sim = 1.0 - d if d is not None else _lexical(terms, h.a.value)
        out.append(_hit(h, ns, sim, input, lexical=d is None))
    return out, wire


def _wanted(h: reads.Held, input: MemorySearchInput) -> bool:
    t = h.a.memory_type
    if not is_searchable_type(t):
        return False
    if input.memory_type is not None:
        if t != input.memory_type:
            return False
    elif t in HIDDEN_FROM_UNFILTERED_SEARCH:
        return False
    if input.tags and not input.tags_soft and not set(input.tags) & set(h.a.tags):
        return False
    if input.session_ids and h.session_id not in input.session_ids:
        return False
    return h.memory_key not in input.exclude_keys


def _lexical(terms: set[str], value: str) -> float:
    """Share of the query's words the value holds, scaled below any real cosine
    match so a pending vector never outranks one that exists."""
    if not terms:
        return 0.0
    words = set(_WORD.findall(value.lower()))
    return 0.5 * len(terms & words) / len(terms)


def _hit(
    h: reads.Held, ns: str, sim: float, input: MemorySearchInput, *, lexical: bool
) -> _MemorySearchHit:
    a = h.a
    hit = _MemorySearchHit(
        memory_key=h.memory_key,
        memory_type=a.memory_type,
        content_excerpt=build_excerpt(a.value, memory_key=h.memory_key),
        distance=1.0 - sim,
        similarity=sim,
        rank_score=sim,
        valid_at=a.valid_from.isoformat() if a.valid_from else "",
        status=a.status,
        version=a.source_version,
        namespace=ns,
        session_id=h.session_id,
        user_id=h.user_id,
        source_class=a.source_class,
        verified=attests(a.verified_by, a.verified_sha256, a.content_sha256),
        assertion_id=a.assertion_id,
        value_sha256=a.value_sha256,
        score_components={"lexical": 1.0} if lexical else {},
    )
    if input.include_tags:
        hit.tags = list(a.tags)
    return hit


async def _shared_hits(
    input: MemorySearchInput, ns: str, axes: Axes
) -> tuple[list[_MemorySearchHit], TemporalCutoff]:
    """Shared memory as promoted at K: each key's version current at K, from the
    shared history mirror. Shared rows carry no valid-time interval, and their past
    vectors are not kept, so they rank lexically (`vectors_unavailable`)."""
    from server.memory.app.blacklist_guard import find_match

    known_ns = to_epoch_ns(axes.known) if axes.known is not None else 2**63 - 1
    rows = await store.shared_versions_at(
        input.org, ns, known_ns, erased=await reads.erased(input.org, ns), limit=_SHARED_LIMIT
    )
    terms = set(_WORD.findall(input.query.lower()))
    out = []
    for r in rows:
        if r.get("tombstoned_at") is not None:
            continue
        value = str(r["value"])
        t = str(r["memory_type"])
        if input.memory_type is not None and t != input.memory_type:
            continue
        # Today's blacklist holds at every K, as on the private path.
        if await find_match(org=input.org, namespace=ns, value=value) is not None:
            continue
        sim = _lexical(terms, value)
        out.append(
            _MemorySearchHit(
                memory_key=str(r["memory_key"]),
                memory_type=t,
                content_excerpt=build_excerpt(value, memory_key=str(r["memory_key"])),
                distance=1.0 - sim,
                similarity=sim,
                rank_score=sim,
                status="active",
                version=int(r["version"]),
                namespace=ns,
                source="shared",
                score_components={"lexical": 1.0},
            )
        )
    gaps = ["shared_history_mirror", "vectors_unavailable", "ranking_current"]
    if len(rows) >= _SHARED_LIMIT:
        gaps.append(f"truncated:{_SHARED_LIMIT}")
    cut = TemporalCutoff(
        namespace=ns, valid_as_of=axes.valid, known_as_of=axes.known, complete=False, gaps=gaps
    )
    return out, cut


# Shared keys read per historical search; past it the answer says it is truncated.
_SHARED_LIMIT = 10_000


def _record_exposure(
    input: MemorySearchInput, hits: list[_MemorySearchHit], cuts: list[TemporalCutoff]
) -> None:
    """One exposure row per namespace: the keys and the exact revisions shown."""
    from server.memory.app.exposure_ledger import Receipt, record_exposure

    for cut in cuts:
        shown = [h for h in hits if h.namespace == cut.namespace]
        record_exposure(
            operation="memory.search",
            org=input.org,
            namespace=cut.namespace,
            keys=[h.memory_key for h in shown],
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            run_id=input.run_id,
            query=input.query,
            top_k=input.top_k,
            receipt=Receipt(
                ids=tuple(h.assertion_id for h in shown),
                sha256=tuple(h.value_sha256 for h in shown),
                cutoff_seq=cut.cutoff_seq,
                valid_as_of=cut.valid_as_of,
                known_as_of=cut.known_as_of,
            ),
        )
