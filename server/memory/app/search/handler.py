"""`handle_memory_search`, the retrieval orchestrator: a sequence of phase and stage
calls, each taking a `SearchContext` (see `context.py`) rather than
sharing enclosing locals."""

from __future__ import annotations

import asyncio
from typing import Any

from server.core.ch import sequential_reads
from server.embedding import get_embedder
from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput
from server.memory.app.search import pg_validation
from server.memory.app.search.anchor_pool import _run_anchor_pool
from server.memory.app.search.assemble import _assemble_final_hits
from server.memory.app.search.budget import SearchBudget
from server.memory.app.search.computed import (
    _computed_answer_inputs,
    _floor_and_computed_evidence,
)
from server.memory.app.search.consistency import _consistency_reread, _fresh_namespace_reread
from server.memory.app.search.context import SearchContext
from server.memory.app.search.embed import _embed_search_query, _embed_text_and_window
from server.memory.app.search.evidence import _dedup_redundant_chunks
from server.memory.app.search.fallbacks import _maybe_iterative_retry
from server.memory.app.search.graph import (
    _count_graph_evidence,
    _run_graph_stages,
)
from server.memory.app.search.key_match import _run_key_match_pool, key_match_enabled
from server.memory.app.search.output import (
    _build_search_output,
    _finish_search_output,
    _serve_parked_page,
    _shape_final_hits,
)
from server.memory.app.search.plan import (
    _collect_set_expansion,
    _plan_retrieval,
    _resolve_query_enrichment,
    _resolve_rerank_endpoint,
    _size_candidate_pool,
)
from server.memory.app.search.query_plan import _is_fast_posture, _is_llm_free
from server.memory.app.search.ranking import (
    _apply_chronological_sort,
    _apply_global_fusion,
    _apply_mention_boost,
    _run_external_rerank,
)
from server.memory.app.search.request import _resolve_search_input
from server.memory.app.search.scope import (
    _launch_scope_reads,
    _prelaunch_canonical_lookup,
    _resolve_entity_scope,
    _SearchRun,
)
from server.memory.app.search.side_legs import (
    _launch_entity_boost,
    _launch_entity_summary_leg,
    _launch_observation_leg,
    _launch_summary_leg,
)
from server.memory.app.search.supplemental import _run_set_expansion, _run_sub_queries
from server.memory.app.search.telemetry import (
    _schedule_auto_touch,
    _staged,
    _start_actual_work,
)
from server.memory.app.search.vector_legs import _run_vector_legs
from server.memory.app.search.verdict import _calibrate_final_hits


async def _run_retrieval_stages(
    ctx: SearchContext,
    _entity_boost_task: asyncio.Task[dict[tuple[str, str], float]] | None,
    _summary_task: asyncio.Task[Any],
    _observation_task: asyncio.Task[Any] | None,
    _entity_summary_task: asyncio.Task[Any] | None,
) -> None:
    """Phase 16: the retrieval pipeline over ctx, in order — vector legs,
    sub-queries, set expansion, anchor and key-match pools, global fusion,
    mention boost, chronological sort, rerank, chunk dedup, the assembler
    (which joins the side legs launched in phase 15), the iterative retry
    (recall credit is the orchestrator's, after the re-read decision). Leaves the served
    block in ctx.final_hits."""
    input = ctx.input
    actual_work = ctx.actual_work
    _clock = ctx.clock
    # Main vector legs (with private-first fallback). The entity-ANN boost task overlaps the
    # legs and is joined inside, just before the pool is scored.
    _clock.mark("legs_launch")
    await _run_vector_legs(ctx, _entity_boost_task)
    _clock.mark("vector_legs")

    # Query decomposition: run sub-queries and merge into the pool.
    await _staged(ctx, "sub_queries", _run_sub_queries(ctx), active=bool(ctx.sub_queries))
    _clock.mark("sub_queries")

    # Set-question instance legs: additive, capped, thresholded.
    await _staged(
        ctx,
        "set_expansion",
        _run_set_expansion(ctx),
        active=bool(ctx.set_expansion_phrases),
    )
    _clock.mark("set_expansion")

    # Entity/temporal anchor additive pool.
    _not_fast = getattr(input, "recipe", "") != "fast"
    await _staged(
        ctx,
        "anchor_pool",
        _run_anchor_pool(ctx),
        active=bool((ctx.effective_entity_filter or ctx.resolved_dates) and _not_fast),
    )
    _clock.mark("anchor_pool")

    # Key-match additive pool: rows whose memory_key names what the question asks for,
    # whatever the search mode.
    await _staged(
        ctx,
        "key_match",
        _run_key_match_pool(ctx),
        active=bool(key_match_enabled(input) and _not_fast),
    )
    _clock.mark("key_match")

    # Single global fusion pass over the COMPLETE pool (main + sub-query +
    # anchor arms). Runs unconditionally so the main pool is always fused on
    # the same path.
    _apply_global_fusion(ctx)

    # Pool-consensus mention boost, default weight 0.
    await _apply_mention_boost(ctx)

    # Comparative-order intent: order by date within the cosine candidate set.
    _apply_chronological_sort(ctx)

    # Pluggable external rerank (operator endpoint), the one relevance authority.
    _clock.mark("fusion")
    await _run_external_rerank(ctx)
    _clock.mark("rerank")

    # Chunk redundancy dedup over the rank-ordered pool, before the top_k
    # slice so the saving shows at every cutoff.
    ctx.all_hits = _dedup_redundant_chunks(ctx.all_hits)

    actual_work["candidates_before_final"] = len(ctx.all_hits)

    # Additive context-block assembler: fold the curated legs in, slice top_k.
    await _assemble_final_hits(
        ctx,
        _summary_task,
        _observation_task,
        _entity_summary_task,
    )

    # Iterative retrieval gate (quality recipe only): one follow-up search.
    _clock.mark("assemble")
    await _maybe_iterative_retry(ctx)
    _clock.mark("iterative_retry")


async def handle_memory_search(input: MemorySearchInput) -> MemorySearchOutput:
    if getattr(input, "page", ""):
        return await _serve_parked_page(input)

    input, caller_hyde_explicit, _top_k_ceiling, search_budget = await _resolve_search_input(input)

    # A reread re-enters on the capped input, where the cap lowers nothing: the ceiling the
    # caller was held to is this pass's.
    fresh_result = await _fresh_namespace_reread(input)
    if fresh_result is not None:
        fresh_result.top_k_ceiling = _top_k_ceiling
        return fresh_result

    # Fast posture skips set expansion (the fast budget also pins the 1x pool); the LLM
    # gates read _is_llm_free.
    fast_posture = _is_fast_posture(input)

    embedder = get_embedder()
    actual_work, _clock = await _start_actual_work(input)
    run = _launch_scope_reads(input)
    # Every task this pass launches is cancelled on the way out if it is still running.
    try:
        (
            obs_date,
            parsed_intent,
            (
                resolved_dates,
                chronological_intent,
                recency_intent,
                preference_attribute,
                llm_query_entities,
                sub_queries,
                query_shape,
            ),
        ) = await _resolve_query_enrichment(input, actual_work)

        set_expansion_phrases = _collect_set_expansion(input, fast_posture, actual_work)

        (
            _derived_aggregate_cache,
            _stated_value_update,
            _parser_derived_intent,
            _temporal_diff_cache,
            _temporal_diff_spec,
        ) = _computed_answer_inputs(input, parsed_intent, search_budget)

        input, search_plan = _plan_retrieval(
            input,
            caller_hyde_explicit,
            query_shape,
            resolved_dates,
            preference_attribute,
            llm_query_entities,
            sub_queries,
        )
        date_suffix, hyde_query, query_anchor_iso = _embed_text_and_window(
            input, resolved_dates, obs_date, actual_work
        )

        await _prelaunch_canonical_lookup(input, run, llm_query_entities, actual_work)

        query_vec = await _embed_search_query(
            input, embedder, hyde_query, date_suffix, search_budget, actual_work, _clock
        )

        await _resolve_entity_scope(input, run, llm_query_entities, actual_work)
        await _admit(input, run, search_budget)

        (
            input,
            search_plan,
            endpoint,
            prompt_template,
        ) = await _resolve_rerank_endpoint(input, search_plan, run, actual_work)

        search_plan, pool_top_k, retrieval_cost_estimate = _size_candidate_pool(
            input,
            search_plan,
            run,
            query_shape,
            sub_queries,
            endpoint,
            actual_work,
        )

        # Assemble the shared pipeline state. From here on the stages read and mutate `ctx`
        # instead of the enclosing locals.
        ctx = SearchContext(
            input=input,
            search_budget=search_budget,
            embedder=embedder,
            llm_free=_is_llm_free(input),
            obs_date=obs_date,
            parsed_intent=parsed_intent,
            resolved_dates=resolved_dates,
            chronological_intent=chronological_intent,
            recency_intent=recency_intent,
            preference_attribute=preference_attribute,
            llm_query_entities=llm_query_entities,
            sub_queries=sub_queries,
            query_shape=query_shape,
            set_expansion_phrases=set_expansion_phrases,
            stated_value_update=_stated_value_update,
            parser_derived_intent=_parser_derived_intent,
            temporal_diff_spec=_temporal_diff_spec,
            query_vec=query_vec,
            search_plan=search_plan,
            namespaces=run.namespaces,
            namespace_order_authoritative=run.order_authoritative,
            effective_entity_filter=run.effective_entity_filter,
            query_anchor_iso=query_anchor_iso,
            pool_top_k=pool_top_k,
            endpoint=endpoint,
            prompt_template=prompt_template,
            actual_work=actual_work,
            clock=_clock,
            derived_aggregate_cache=_derived_aggregate_cache,
            temporal_diff_cache=_temporal_diff_cache,
        )

        # Graph-mode pre-expansion (entity 1-hop) and relation-typing triples,
        # side by side: neither reads what the other writes.
        _clock.mark("scope")
        await _run_graph_stages(ctx)
        _clock.mark("graph")

        _entity_boost_task = _launch_entity_boost(ctx)

        # Curated-block retrieval legs, fanned out concurrently with the main
        # vector legs and joined just before the assembler:
        #   • Session-summary leg: top-N dense session digests per namespace,
        #     folded into the pool (RRF-fused, NOT pinned). Fail-open [].
        #   • Current-state observation leg: latest-dated value of one
        #     single-valued attribute (LLM-free CH window function), gated on
        #     stated_value_update. A resolved exact answer → tier-0 pinned.
        _summary_task = _launch_summary_leg(ctx)
        run.side_tasks = [_entity_boost_task, _summary_task]
        _entity_summary_task = await _launch_entity_summary_leg(ctx)
        _observation_task = _launch_observation_leg(ctx)
        run.side_tasks += [_entity_summary_task, _observation_task]

        await _run_retrieval_stages(
            ctx,
            _entity_boost_task,
            _summary_task,
            _observation_task,
            _entity_summary_task,
        )

        _count_graph_evidence(ctx, run)

        # Adaptive thresholds, three-tier: per-namespace calibration override →
        # cluster-wide boot-probe calibration → scalar settings fallback.
        # Resolved ONCE per search to avoid N+1 lookups.
        run.ns_calibration = await run.calibration_task
        run.top_k_ceiling = _top_k_ceiling
        run.retrieval_cost_estimate = retrieval_cost_estimate

        final_hits = await pg_validation.validate(ctx)

        reread_result = await _consistency_reread(ctx, final_hits)
        if reread_result is not None:
            reread_result.top_k_ceiling = _top_k_ceiling
            return reread_result  # the re-read pass credited what it serves

        # Auto-touch: recall-usage events, if this reader is credited — for the pass that is
        # served, once (a pass merged into its parent leaves it to the parent).
        if not getattr(input, "_merged_into_parent", False):
            await _schedule_auto_touch(ctx)

        final_hits, derived_aggregate, evidence_support_count = await _floor_and_computed_evidence(
            ctx, run, final_hits
        )

        calibration, abstain_verdict = _calibrate_final_hits(
            ctx, run, final_hits, evidence_support_count
        )

        final_hits, _unit_bodies, _namespaces_searched = await _shape_final_hits(ctx, final_hits)
        _output = _build_search_output(
            ctx,
            run,
            final_hits,
            _namespaces_searched,
            _unit_bodies,
            derived_aggregate,
            calibration,
            abstain_verdict,
        )
        await _finish_search_output(ctx, _output, final_hits)
        return _output
    finally:
        run.close()


async def _admit(input: MemorySearchInput, run: _SearchRun, search_budget: SearchBudget) -> None:
    """The Postgres barrier (`pg_validation.admit`). A re-entrant pass (a fallback, a
    re-read) waits no longer than its parent's time left, a first pass the barrier's own
    bound; a search that waited reads sequentially for the rest of the pass."""
    cap_s = search_budget.wall_left_s() if getattr(input, "_pass_depth", 0) else None
    if await pg_validation.admit(input.org, run.namespaces, cap_s):
        run.sequential_scope.enter_context(sequential_reads())
