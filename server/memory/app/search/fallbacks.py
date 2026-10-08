"""The recursive fallback on a weak first pass: the iterative judge, bounded by the
shared SearchBudget.

It re-enters through the package's `handle_memory_search`, imported at call
time: the handler imports this module."""

from __future__ import annotations

import asyncio
import logging

from server.memory.app.search.context import SearchContext
from server.memory.domain.search_types import hit_sort_key

log = logging.getLogger(__package__)


async def _maybe_iterative_retry(ctx: SearchContext) -> None:
    """Iterative retrieval gate. Ask the LLM "did we get it?"; if not, run ONE
    follow-up search with its suggested query and merge. Quality-recipe only
    (the default path's abstain / retrieval_confidence already carry the
    signal). Gated on a mid-range top_similarity — strong and weak
    retrievals skip."""
    input = ctx.input
    final_hits = ctx.final_hits
    _iter_top_sim = (1.0 - final_hits[0].distance) if final_hits else 0.0
    if not (
        getattr(input, "recipe", "") == "quality"
        and 0.35 <= _iter_top_sim <= 0.65
        # Chronological intent: the date IS the answer and the chronological sort owns the
        # final order. This leg merges a recursive result and re-sorts by rank_score, which
        # would clobber that date order.
        and not ctx.chronological_intent
        # One level of re-entry: a re-entered pass never retries again.
        and getattr(input, "_pass_depth", 0) == 0
        # Fast posture buys no LLM-judge latency.
        and not ctx.fast_posture
        # The judge's call is paid only when the round it could lead to is affordable: a
        # judge whose "retry" the budget would refuse is an LLM call for nothing.
        and ctx.search_budget.fallback_affordable(
            rounds=1, llm_calls=1, vector_queries=len(ctx.namespaces)
        )
        # Split spend: the judge reserves only its own LLM unit here; the
        # fallback round + vector work is reserved below only on rejection.
        and ctx.search_budget.try_spend_llm(1)
    ):
        return
    from server.memory.app.search import handle_memory_search
    from server.memory.app.search_llm import judge_results_iteratively

    ctx.actual_work["llm_iterative_judge_calls"] = 1
    ctx.search_budget.note_llm_call(1)
    excerpts = [h.content_excerpt for h in final_hits[:6]]
    answered, suggested = await judge_results_iteratively(
        input.query,
        excerpts,
    )
    if (
        not answered
        and suggested
        and suggested != input.query
        # Round + vector work spent only on rejection (see above).
        and ctx.search_budget.try_spend_fallback(
            rounds=1,
            vector_queries=len(ctx.namespaces),
        )
    ):
        log.info(
            "memory.search: iterative judge said retrieval didn't answer; retrying with %r",
            suggested,
        )
        try:
            retry_input = input.model_copy(
                update={
                    "query": suggested,
                    "_pass_depth": 1,
                    "_merged_into_parent": True,
                },
            )
            # Recursion bounded by the budget's remaining wall time
            # so an in-flight retry can't blow past the cascade cap.
            retry_out = await asyncio.wait_for(
                handle_memory_search(retry_input),
                timeout=ctx.search_budget.remaining_wall_s(),
            )
            # Merge dedup at the content-aware grain (namespace,
            # memory_key, excerpt) — matching the sub-query / anchor merge.
            seen_keys = {
                (h.namespace or input.namespace, h.memory_key, h.content_excerpt)
                for h in final_hits
            }
            for rh in retry_out.results:
                rh_key = (
                    rh.namespace or input.namespace,
                    rh.memory_key,
                    rh.content_excerpt,
                )
                if rh_key not in seen_keys:
                    seen_keys.add(rh_key)
                    final_hits.append(rh)
            final_hits.sort(key=hit_sort_key)
            ctx.final_hits = final_hits[: input.top_k]
        except Exception as e:  # noqa: BLE001
            log.warning("iterative retrieve retry failed: %s", e)
