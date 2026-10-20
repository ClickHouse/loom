"""Planning a search: the query's intent signals, the query planner's choice of
mode / HyDE / planes, the relevance authority, and the first-stage pool size."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.query_plan import (
    _enrichment_parser_gate_active,
    _is_llm_free,
    _should_run_query_enrichment,
)
from server.memory.app.search.scope import _SearchRun
from server.memory.domain import set_expansion as _set_expansion
from server.memory.domain.rerank_names import BUILTIN_OPENAI

if TYPE_CHECKING:
    from server.memory.domain.query_intent import QueryIntent
    from server.memory.domain.query_planner import MemorySearchPlan


def _quality_pool(input: Any, pool_top_k: int, settings_obj: Any) -> int:
    """The pool cap (it bounds the widening multiplier, never the caller's top_k).

    A quality-filtered search widens the pool by `memory_search_quality_pool_multiplier` first:
    in a namespace where most of a fact leg's rows are junk the filter drops, the real answer
    often sits just below the cut, so filtering an unwidened pool leaves the window to
    transcript chunks."""
    if getattr(input, "quality_filter", False):
        pool_top_k *= max(1, int(settings_obj.memory_search_quality_pool_multiplier))
    return min(pool_top_k, max(int(settings_obj.memory_search_max_pool_top_k), int(input.top_k)))


# resolved_dates, chronological_intent, recency_intent, preference_attribute,
# llm_query_entities, sub_queries, query_shape — QueryIntent.enrichment_signal().
_EnrichmentSignal = tuple[list[str], str, bool, str, list[str], list[str], str]


async def _resolve_query_enrichment(
    input: MemorySearchInput,
    actual_work: dict[str, Any],
) -> tuple[str | None, QueryIntent | None, _EnrichmentSignal]:
    """Phase 5: the query's intent signals from the deterministic parser, else the empty
    signal. Returns the observation date, the parsed intent (None when the parser did not run
    or abstained) and the signal tuple."""
    obs_date = input.observation_date or None
    # The parser always runs under fast posture (a confident spec is free signal the fast
    # path otherwise lacks); every other posture defers to `_should_run_query_enrichment`.
    # It abstains on free-form queries, which then take the empty signal: the read path is
    # LLM-free.
    parsed_intent = None
    if _enrichment_parser_gate_active(input) or _should_run_query_enrichment(input):
        from server.memory.domain.query_intent import parse_query_intent

        parsed_intent = parse_query_intent(input.query, obs_date)
    # two ledger names for one fact; both ride retrieval_work
    actual_work["intent_parser_used"] = actual_work["query_enrichment_used"] = (
        parsed_intent is not None
    )
    if parsed_intent is None:
        empty: _EnrichmentSignal = ([], "", False, "", [], [], "")
        return obs_date, None, empty
    return obs_date, parsed_intent, parsed_intent.enrichment_signal()


def _collect_set_expansion(
    input: MemorySearchInput, fast_posture: bool, actual_work: dict[str, Any]
) -> list[str]:
    """Phase 6: the set-question instance phrases, de-duplicated — the parser's deterministic
    collective-noun phrases, no model call, which ride the sub-query fusion as extra vector
    legs. None under fast posture."""
    phrases = (
        [] if fast_posture else list(dict.fromkeys(_set_expansion.collective_phrases(input.query)))
    )
    actual_work["set_expansion_phrases"] = len(phrases)
    return phrases


def _plan_retrieval(
    input: MemorySearchInput,
    caller_hyde_explicit: bool,
    query_shape: str,
    resolved_dates: list[str],
    preference_attribute: str,
    llm_query_entities: list[str],
    sub_queries: list[str],
) -> tuple[MemorySearchInput, MemorySearchPlan]:
    """Phase 7: the query planner picks mode / HyDE / unmatched / rerank from
    the intent signals. Returns the input with the plan's updates applied
    and the plan."""
    from server.memory.domain.query_planner import plan_memory_search

    initial_hyde_allowed = bool(getattr(input, "hyde", False)) and (
        caller_hyde_explicit
        or getattr(input, "retrieval_budget", "standard") == "deep"
        or getattr(input, "recipe", "") == "quality"
    )
    search_plan = plan_memory_search(
        current_search_mode=input.search_mode,
        current_hyde=initial_hyde_allowed,
        current_include_top_n_unmatched=getattr(input, "include_top_n_unmatched", 0),
        current_rerank=getattr(input, "rerank", "") or "",
        recipe=getattr(input, "recipe", "") or "",
        query_shape=query_shape,
        resolved_dates=resolved_dates,
        preference_attribute=preference_attribute,
        entity_count=len(llm_query_entities),
        sub_query_count=len(sub_queries),
        budget_class=getattr(input, "retrieval_budget", "standard"),
        query_text=input.query,
        mode_pinned=not bool(getattr(input, "_search_mode_defaulted", False)),
        namespace_defaults=tuple(getattr(input, "_namespace_defaults_applied", ()) or ()),
    )
    if _is_llm_free(input):
        # The plan an LLM-free read reports runs no LLM step, whatever the planner chose
        # (the deep budget turns HyDE on after `current_hyde`).
        search_plan = replace(
            search_plan,
            hyde=False,
            rerank="" if search_plan.rerank.startswith(BUILTIN_OPENAI) else search_plan.rerank,
        )
    input = input.model_copy(update=search_plan.input_updates)
    return input, search_plan


async def _resolve_rerank_endpoint(
    input: MemorySearchInput,
    search_plan: MemorySearchPlan,
    run: _SearchRun,
    actual_work: dict[str, Any],
) -> tuple[MemorySearchInput, MemorySearchPlan, str, str]:
    """Phase 12: the relevance authority. Awaits the namespace rerank config
    (or takes the request override); without an external reranker the
    planner's unmatched plane is pruned. Returns the (possibly re-planned)
    input and plan, the endpoint ("" for none) and its prompt template."""

    if run.rerank_cfg_task is None:
        endpoint = "" if run.rerank_override == "none" else run.rerank_override
        prompt_template = ""
    else:
        endpoint, prompt_template = await run.rerank_cfg_task
    if endpoint.startswith(BUILTIN_OPENAI) and _is_llm_free(input):
        # The built-in reranker is an LLM call, which an LLM-free read never makes.
        endpoint, prompt_template = "", ""
    if search_plan.include_top_n_unmatched > 0 and not endpoint:
        search_plan = replace(
            search_plan,
            include_top_n_unmatched=0,
            planes=[p for p in search_plan.planes if p != "unmatched"],
            reason=f"{search_plan.reason}:unmatched_pruned_no_rerank",
        )
        input = input.model_copy(update=search_plan.input_updates)
    return input, search_plan, endpoint, prompt_template


def _size_candidate_pool(
    input: MemorySearchInput,
    search_plan: MemorySearchPlan,
    run: _SearchRun,
    query_shape: str,
    query_enrichment_used: bool,
    sub_queries: list[str],
    endpoint: str,
    actual_work: dict[str, Any],
) -> tuple[MemorySearchPlan, int, dict[str, Any]]:
    """Phase 13: first-stage pool size (budget / rerank / shape multipliers,
    capped) and the plan's work estimate. Returns the plan carrying that
    estimate, pool_top_k, and the estimate."""
    from server.memory.domain.query_planner import estimate_memory_search_work
    from server.settings import settings as _settings

    external_rerank_enabled = bool(endpoint)

    namespaces = run.namespaces
    effective_entity_filter = run.effective_entity_filter
    # Pool-K widening. A wide first-stage pool feeding the rerank is the
    # biggest recall lever — the rerank is a precision tool that can't
    # recover what the first stage missed. With a configured reranker,
    # widening is essentially free (same rerank call, larger payload,
    # final top_k unchanged). Shape multiplier widens further for
    # aggregational / temporal_diff / needle shapes.
    _shape_multiplier = 2 if query_shape in ("aggregational", "temporal_diff", "needle") else 1
    budget_class = input.retrieval_budget
    if budget_class == "fast":
        _pool_multiplier = 1
    elif budget_class == "deep":
        _pool_multiplier = int(_settings.memory_search_pool_multiplier_deep)
    elif external_rerank_enabled:
        _pool_multiplier = int(_settings.memory_search_pool_multiplier_rerank)
    else:
        _pool_multiplier = int(_settings.memory_search_pool_multiplier_standard)
    pool_top_k = max(input.top_k * max(1, _pool_multiplier), input.top_k * _shape_multiplier)
    # The pool cap bounds the WIDENING multiplier, never the caller's
    # explicit ask: a top_k above the cap must still pool >= top_k
    # candidates or the fixed-K fill contract (results ~= requested
    # top_k when the namespace has the rows) silently truncates.
    pool_top_k = _quality_pool(input, pool_top_k, _settings)  # the cap; quality-filter widening
    actual_work["pool_top_k"] = int(pool_top_k)
    llm_free = _is_llm_free(input)
    retrieval_cost_estimate = estimate_memory_search_work(
        search_plan,
        namespace_count=len(namespaces),
        top_k=int(input.top_k),
        pool_top_k=int(pool_top_k),
        # The estimate counts enrichment as an LLM call; an LLM-free read's is the parser.
        query_enrichment_used=query_enrichment_used and not llm_free,
        sub_query_count=len(sub_queries),
        entity_count=len(effective_entity_filter),
        graph_triples_enabled=input.search_mode == "graph" and not llm_free,
        external_rerank_enabled=external_rerank_enabled,
        llm_rerank=endpoint.startswith(BUILTIN_OPENAI),
    )
    search_plan = replace(search_plan, estimated_work=retrieval_cost_estimate)
    return search_plan, pool_top_k, retrieval_cost_estimate
