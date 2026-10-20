"""Computed-evidence legs: the derived aggregate and the date difference.

Each is lazy and computed at most once: its result binds into a ctx cache keyed
"v", so the fallback early return and the main return attach the SAME scalar."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from server.core.ch import get_client
from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app.search.budget import SearchBudget
from server.memory.app.search.context import SearchContext
from server.memory.app.search.evidence import (
    _apply_min_similarity_floor,
    _derived_evidence_hit,
    _temporal_diff_evidence_hit,
)
from server.memory.app.search.evidence_support import _evidence_support_count
from server.memory.app.search.knobs import _floor_mode, _noise_floor
from server.memory.app.search.scope import _SearchRun

if TYPE_CHECKING:
    from server.memory.domain.query_intent import QueryIntent

log = logging.getLogger(__package__)


def _read_identity(input: Any) -> dict[str, Any]:
    """The read's identity bound for the derived-fact aggregate, as the fact leg applies it:
    the agent on a private read (a wildcard reads across agents), the user, the session and
    the caller's session set. Without it an own_user / own_session read's scalar and items
    span every user of the namespace."""
    return {
        "agent": input.agent if input.scope == "private" else "*",
        "user_id": input.user_id,
        "session_id": input.session_id,
        "session_ids": list(input.session_ids or []),
    }


async def _maybe_derived_aggregate(ctx: SearchContext) -> dict[str, Any] | None:
    """Derived-answer aggregate leg. Gated on the derived-answer query
    shapes; fail-open to None. An LLM-free read (the fast posture,
    context.pack) makes no LLM call here: it takes only the parser's intent,
    and matches the predicate without LLM synonyms (its deterministic
    predicate class still applies)."""
    input = ctx.input
    cache = ctx.derived_aggregate_cache
    if "v" in cache:
        cached = cache["v"]
        return cached if isinstance(cached, dict) else None
    result: dict[str, Any] | None = None
    if ctx.query_shape in ("aggregational", "temporal_diff") and not ctx.stated_value_update:
        try:
            from server.memory.app.retrieval import fetch_derived_aggregate

            intent = ctx.parser_derived_intent
            if intent is None and not ctx.llm_free:
                from server.memory.app.search_llm import extract_derived_intent_llm

                ctx.search_budget.note_llm_call(1)
                intent = await extract_derived_intent_llm(input.query)
            if intent:
                subj = intent.get("subject") or "user"
                pred = intent.get("predicate") or None
                cat = intent.get("category") or None
                obj = intent.get("object") or None
                # Expand each dimension SEPARATELY through the canonical
                # map — a combined call returns a flat union that would
                # cross-contaminate the per-dimension IN-lists. Fail-open
                # to None; the self-canonical "user" subject needs no lookup.
                from server.memory.app.entity_resolution import (
                    lookup_canonical_entities,
                )

                async def _exp(v: str | None) -> list[str] | None:
                    if not v:
                        return None
                    try:
                        out = await lookup_canonical_entities(
                            get_client(),
                            org=input.org,
                            namespaces=[input.namespace],
                            entity_texts=[v],
                        )
                        return out or None
                    except Exception as e:  # noqa: BLE001
                        log.warning("derived canonical expand failed (non-fatal): %s", e)
                        return None

                cat_expanded, obj_expanded, subj_expanded = await asyncio.gather(
                    _exp(cat), _exp(obj), _exp(subj if subj != "user" else None)
                )
                agg = await fetch_derived_aggregate(
                    get_client(),
                    org=input.org,
                    namespace=input.namespace,
                    subject=subj,
                    predicate=pred,
                    category=cat,
                    object_like=obj,
                    subject_canonical_expanded=subj_expanded,
                    category_canonical_expanded=cat_expanded,
                    object_canonical_expanded=obj_expanded,
                    expand_synonyms=not ctx.llm_free,
                    on_llm_call=ctx.search_budget.note_llm_call,
                    **_read_identity(input),
                )
                # The canonical map may not carry the category surface; if
                # the category filter matched nothing, broaden to
                # predicate-only once rather than abstain. Flagged so the
                # reader can hedge.
                broadened = False
                if agg is None and cat:
                    agg = await fetch_derived_aggregate(
                        get_client(),
                        org=input.org,
                        namespace=input.namespace,
                        subject=subj,
                        predicate=pred,
                        category=None,
                        object_like=obj,
                        subject_canonical_expanded=subj_expanded,
                        object_canonical_expanded=obj_expanded,
                        expand_synonyms=not ctx.llm_free,
                        on_llm_call=ctx.search_budget.note_llm_call,
                        **_read_identity(input),
                    )
                    broadened = agg is not None
                if agg:
                    agg["op"] = intent["op"]
                    agg["subject"] = subj
                    agg["predicate"] = intent.get("predicate") or ""
                    agg["category"] = intent.get("category") or ""
                    agg["category_broadened"] = broadened
                    result = agg
        except Exception as e:  # noqa: BLE001
            log.warning("derived aggregate leg failed (non-fatal): %s", e)
    cache["v"] = result
    return result


async def _maybe_temporal_diff(ctx: SearchContext) -> dict[str, Any] | None:
    """Deterministic date-difference leg. Binds each operand event clause to
    its best-matching dated fact and computes the interval server-side with
    dateDiff. Computed at most once. Fail-open to None."""
    cache = ctx.temporal_diff_cache
    if "v" in cache:
        cached: dict[str, Any] | None = cache["v"]
        return cached
    input = ctx.input
    result: dict[str, Any] | None = None
    if ctx.temporal_diff_spec is not None:
        try:
            from server.memory.app.retrieval import fetch_temporal_diff

            operands = list(ctx.temporal_diff_spec["operands"])
            to_observation = bool(ctx.temporal_diff_spec["to_observation"])
            operand_vecs = await asyncio.gather(*[ctx.embedder.embed(o) for o in operands])
            ctx.actual_work["embedding_calls"] = int(ctx.actual_work["embedding_calls"]) + len(
                operands
            )
            result = await fetch_temporal_diff(
                get_client(),
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id,
                operand_vecs=list(operand_vecs),
                embedding_model=ctx.embedder.model_name,
                observation_date=ctx.obs_date,
                to_observation=to_observation,
                run_id=str(getattr(input, "run_id", "") or ""),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("temporal diff leg failed (non-fatal): %s", e)
    cache["v"] = result
    return result


def _computed_answer_inputs(
    input: MemorySearchInput, parsed_intent: QueryIntent | None, search_budget: SearchBudget
) -> tuple[dict[str, Any], bool, dict[str, Any] | None, dict[str, Any], dict[str, Any] | None]:
    """Phase 6b: what the computed-answer legs (derived aggregate, date
    difference) need — their compute-once caches from the cascade's plan
    cache and the parser's specs. Returns the derived cache, the
    stated-value-update flag, the parser's derived intent, the temporal-diff
    cache and its spec."""
    # Derived-answer aggregate leg. Computed lazily and at most once so the
    # fallback early-return and main return attach the SAME COUNT/SUM/
    # date-diff. Gated on the derived-answer query shapes; fail-open to None.
    _derived_aggregate_cache: dict[str, Any] = search_budget.plan_cache.setdefault(
        ("derived", input.namespace, input.query), {}
    )
    # Stated-value-update shapes ("how many X do I have now") must never
    # route to the COUNT aggregate: the latest stated value answers them,
    # not the number of event rows. The parser's recency_intent already
    # boosts latest-value retrieval for these.
    _stated_value_update = bool(parsed_intent and parsed_intent.stated_value_update)
    _parser_derived_intent = parsed_intent.derived_intent() if parsed_intent is not None else None

    # Deterministic date-difference leg. For date-difference query shapes
    # the reader must not subtract dates itself (LLM date arithmetic is
    # unreliable). The parser resolves the operand event clauses; this leg
    # binds each to its best-matching dated fact's event date and computes
    # the interval server-side with dateDiff. Computed at most once, cached
    # so early-return and main return attach the SAME interval. Fail-open to
    # None — a missing operand falls back to normal recall, never a guess.
    _temporal_diff_cache: dict[str, Any] = search_budget.plan_cache.setdefault(
        ("temporal_diff", input.namespace, input.query), {}
    )
    _temporal_diff_spec = parsed_intent.temporal_diff_spec() if parsed_intent is not None else None
    return (
        _derived_aggregate_cache,
        _stated_value_update,
        _parser_derived_intent,
        _temporal_diff_cache,
        _temporal_diff_spec,
    )


async def _floor_and_computed_evidence(
    ctx: SearchContext, run: _SearchRun, final_hits: list[_MemorySearchHit]
) -> tuple[list[_MemorySearchHit], dict[str, Any] | None, int]:
    """Phase 20: the final noise floor, then the computed-evidence row
    (date difference, else derived aggregate) pinned first, then the
    belief-sources support count. Returns the hits, the derived aggregate
    and the support count."""
    input = ctx.input
    _clock = ctx.clock
    # The noise floor runs last, on the fused and assembled window.
    final_hits = _apply_min_similarity_floor(
        final_hits,
        _noise_floor(input, run.ns_calibration),
        top_k=int(input.top_k),
        mode=_floor_mode(input),
    )
    # Computed-evidence injection: a computed scalar rides results[] as the
    # first row, before the abstain/calibration block so confidence accounts
    # for it. Two legs in precedence order:
    #   1. Deterministic date-difference — correct for date-diff shapes;
    #      suppresses the single-predicate span aggregate when it fires.
    #   2. Derived aggregate (count / sum / single-predicate span).
    temporal_diff = await _maybe_temporal_diff(ctx)
    derived_aggregate = None if temporal_diff else await _maybe_derived_aggregate(ctx)
    _clock.mark("computed")
    if temporal_diff and not any(getattr(h, "source", "") == "computed" for h in final_hits):
        final_hits.insert(
            0,
            _temporal_diff_evidence_hit(temporal_diff, final_hits, namespace=input.namespace),
        )
    elif derived_aggregate and not any(
        getattr(h, "memory_key", "") == "derived_aggregate" for h in final_hits
    ):
        final_hits.insert(
            0,
            _derived_evidence_hit(
                derived_aggregate,
                final_hits,
                namespace=input.namespace,
            ),
        )
    evidence_support_count = 0
    try:
        evidence_support_count = await _evidence_support_count(input, final_hits)
    except Exception as e:  # noqa: BLE001
        log.warning("evidence support lookup failed (non-fatal): %s", e)
    _clock.mark("evidence_support")
    return final_hits, derived_aggregate, evidence_support_count
