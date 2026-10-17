"""The side legs launched ahead of the vector legs and joined by the assembler:
entity-ANN boost, session summaries, entity summaries and the current-state
observation."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.core.background import create_deferred_task
from server.core.ch import get_client
from server.memory.app.search.ann_entity_boosts import _ann_entity_boosts
from server.memory.app.search.context import SearchContext
from server.memory.app.search.scope import _retain_task_result
from server.memory.app.search.vector_legs import _leg_or_empty

# Underscore is IN the class: a consolidated technical entity is often a
# snake_case test/check name ("00084_external_aggregation") — splitting on `_`
# shatters it into "00084"/"external"/"aggregation", none of which match the
# whole-name summary entity, so the per-failure consolidation never surfaces.
_SUMMARY_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")


def _seed_entity_summary_entities(
    query: str,
    effective_entity_filter: list[str] | None,
    known_entities: frozenset[str] | None = None,
) -> list[str]:
    """Entities to scope the pre-built entity-summary read leg to.

    The parsed-intent entity filter is EMPTY on the cheap query path — a
    people/handle query ("what do we know about @jdoe") doesn't trip
    ``_should_run_query_enrichment``, so no entities are extracted. That is
    exactly the query the per-person consolidation targets, so scoping the leg
    on that filter alone would make the read flag a no-op for people queries. Seed it from
    two LLM-free signals:

    1. Salient @handles (``_salient_identifiers``), canonicalised to the stored
       entity form. Numbers (PR/issue ids) are excluded.
    2. Query word-tokens that match ``known_entities`` — the set of entities the
       summary plane actually holds. This covers BARE maintainer names
       ("jdoe", "jane-smith") a query names without an ``@`` AND snake_case
       technical entities ("00084_external_aggregation") whose per-failure
       consolidation links the failure to its fix-PR/commit; matching against the
       real vocabulary keeps it precise (a non-entity word can't match).
    """
    from server.memory.domain.graph_quality import canonical_entity_text
    from server.memory.domain.retrieval_calibration import _salient_identifiers

    out: list[str] = list(effective_entity_filter or [])
    seen = {e.lower() for e in out}

    def _add(canon: str) -> None:
        if canon and canon.lower() not in seen:
            seen.add(canon.lower())
            out.append(canon)

    for raw in _salient_identifiers(query or ""):
        if raw.startswith("@"):
            _add(canonical_entity_text(raw))

    if known_entities:
        for tok in _SUMMARY_TOKEN_RE.findall((query or "").lower()):
            if tok in known_entities:
                _add(canonical_entity_text(tok))
    return out


def _launch_entity_boost(
    ctx: SearchContext,
) -> asyncio.Task[dict[tuple[str, str], float]] | None:
    """Phase 15a: launch the entity-store ANN boost read (None without query
    entities); joined inside the vector legs."""
    input = ctx.input
    actual_work = ctx.actual_work
    embedder = ctx.embedder
    # Entity-store ANN read, launched concurrently with the main vector
    # legs (depends only on query entities + namespaces). Catches semantic
    # entity matches (query "lab" → memory entity "laboratory") that the
    # entity_filter hasAny() path misses.
    _entity_boost_task: asyncio.Task[dict[tuple[str, str], float]] | None = None
    if ctx.effective_entity_filter:
        actual_work["entity_ann_queries"] = len(ctx.effective_entity_filter)
        actual_work["embedding_calls"] = int(actual_work["embedding_calls"]) + len(
            ctx.effective_entity_filter
        )
        _entity_boost_task = create_deferred_task(
            _ann_entity_boosts(
                org=input.org,
                namespaces=ctx.namespaces,
                agent=input.agent,
                user_id=input.user_id,
                query_entities=ctx.effective_entity_filter,
                embedder=embedder,
            )
        )
        _entity_boost_task.add_done_callback(_retain_task_result)
    return _entity_boost_task


def _launch_summary_leg(ctx: SearchContext) -> asyncio.Task[Any]:
    """Phase 15b: launch the session-summary leg (empty unless the request
    wants the sessions leg); joined by the assembler."""
    from server.memory.app.retrieval import fetch_session_summaries
    from server.observability.otel import traced

    input = ctx.input
    embedder = ctx.embedder

    async def _summary_legs() -> list[Any]:
        with traced("ch.query.session_summaries", namespaces=len(ctx.namespaces)):
            per_ns = await asyncio.gather(
                *[
                    fetch_session_summaries(
                        get_client(),
                        org=input.org,
                        namespace=ns,
                        agent=input.agent,
                        user_id=input.user_id,
                        query_vec=ctx.query_vec,
                        embedding_model=embedder.model_name,
                        as_of=input.as_of,
                        as_of_basis=getattr(input, "as_of_basis", "valid_time"),
                        # The digest leg keeps to the run and the read's session bound the
                        # way the fact and chunk legs do.
                        run_id=input.run_id,
                        session_id=input.session_id,
                        session_ids=list(input.session_ids or []),
                    )
                    for ns in ctx.namespaces
                ]
            )
        out: list[Any] = []
        for ns, hits in zip(ctx.namespaces, per_ns, strict=True):
            for h in hits:
                out.append((ns, h))
        return out

    _summary_task = create_deferred_task(_leg_or_empty(input, "sessions", _summary_legs))
    _summary_task.add_done_callback(_retain_task_result)
    return _summary_task


async def _launch_entity_summary_leg(ctx: SearchContext) -> asyncio.Task[Any] | None:
    """Phase 15c: launch the pre-built entity-summary leg (flag-gated, never
    on a run-scoped read); None when it does not open."""
    from server.memory.app.retrieval import fetch_entity_summaries

    input = ctx.input
    embedder = ctx.embedder
    # PRE-BUILT entity-summary leg. When the query carries entities, fetch
    # the offline-worker-built per-entity summaries for those entities (the entity_filter is
    # already canonical/alias-expanded) and fold them in as source="entity_summary" ADDITIVE
    # dense rows — RRF-fused like session summaries, NEVER pool-displacing. It is a PURE CH
    # read of pre-built rows — NO LLM on the hot path, so the leg is latency-flat. Launched
    # concurrently with the other legs.
    from server.settings import settings as _es_settings

    # The parsed-intent entity filter (ctx.effective_entity_filter) is EMPTY on the cheap
    # query path — a "what do we know about @jdoe" people/handle query doesn't trip
    # `_should_run_query_enrichment`, so no entities are extracted. That is exactly the query
    # the per-person consolidation targets, so gating this leg on that filter alone would make
    # it a no-op for people queries. Seed it from two LLM-free signals: salient
    # @handles, and query word-tokens that match the summary plane's own entity vocabulary
    # (covers bare maintainer names named without an @). The vocabulary read is cached.
    _es_entities: list[str] = []
    # Entity summaries are namespace-wide aggregates no run wrote, so a run-scoped read does
    # not open that leg at all.
    if input.run_id is None:
        from server.memory.app.retrieval import summary_entities_present

        _known = await summary_entities_present(get_client(), input.org, ctx.namespaces)
        _es_entities = _seed_entity_summary_entities(
            input.query, ctx.effective_entity_filter, known_entities=_known
        )

    _entity_summary_task: asyncio.Task[Any] | None = None
    if _es_entities:
        _entity_summary_top_n = int(_es_settings.memory_search_entity_summaries_top_n)

        async def _entity_summary_legs() -> list[Any]:
            per_ns = await asyncio.gather(
                *[
                    fetch_entity_summaries(
                        get_client(),
                        org=input.org,
                        namespace=ns,
                        agent=input.agent,
                        user_id=input.user_id,
                        query_vec=ctx.query_vec,
                        embedding_model=embedder.model_name,
                        entity_filter=_es_entities,
                        n=_entity_summary_top_n,
                        as_of=input.as_of,
                        as_of_basis=getattr(input, "as_of_basis", "valid_time"),
                    )
                    for ns in ctx.namespaces
                ]
            )
            out: list[Any] = []
            for ns, hits in zip(ctx.namespaces, per_ns, strict=True):
                for h in hits:
                    out.append((ns, h))
            return out

        _entity_summary_task = create_deferred_task(_entity_summary_legs())
        _entity_summary_task.add_done_callback(_retain_task_result)
    return _entity_summary_task


def _launch_observation_leg(ctx: SearchContext) -> asyncio.Task[Any] | None:
    """Phase 15d: launch the current-state observation leg for a
    stated-value-update question; None otherwise."""
    from server.memory.app.retrieval import fetch_current_state_observation

    input = ctx.input
    embedder = ctx.embedder
    _stated_value_update = ctx.stated_value_update
    _observation_task: asyncio.Task[Any] | None = None
    if _stated_value_update:

        async def _observation_legs() -> list[Any]:
            per_ns = await asyncio.gather(
                *[
                    fetch_current_state_observation(
                        get_client(),
                        org=input.org,
                        namespace=ns,
                        agent=input.agent,
                        user_id=input.user_id,
                        query_vec=ctx.query_vec,
                        embedding_model=embedder.model_name,
                        entity_filter=ctx.effective_entity_filter,
                        as_of=input.as_of,
                        as_of_basis=getattr(input, "as_of_basis", "valid_time"),
                    )
                    for ns in ctx.namespaces
                ]
            )
            out: list[Any] = []
            for ns, hits in zip(ctx.namespaces, per_ns, strict=True):
                for h in hits:
                    out.append((ns, h))
            return out

        _observation_task = create_deferred_task(_observation_legs())
        _observation_task.add_done_callback(_retain_task_result)
    return _observation_task
