"""`ask`: the memory.ask phases in order, the precedent fast paths and the
synthesized result."""

from __future__ import annotations

import asyncio
from typing import Any

from server.core.background import create_deferred_task
from server.memory.app.ask.abstain import _early_answer
from server.memory.app.ask.cascade import (
    _answer_with_deep_pass,
    _AskAnswer,
    _audit_answer,
    _mid_verify,
    _route_tier,
)
from server.memory.app.ask.context_blocks import knowledge_passages, recent_turns_block
from server.memory.app.ask.pool import (
    _ask_pool_size,
    _AskRun,
    _complete_bridges,
    _complete_facets,
    _complete_options,
    _ground_and_cut,
    _inject_named_evidence,
    _omitted_rows,
    _order_pool,
    _read_time,
    _retrieve,
    _served_pool,
    _settle_window,
    _tag_belt,
    _widen_by_question,
)
from server.memory.app.ask.reader import _answer_chars, _AskReader, _check_premise
from server.memory.app.ask.result import (
    MemoryAskResult,
    _keys_of,
    answer_grounding,
    pool_composition,
)
from server.memory.app.ask.state import (
    _LAST_COUNT_RECOMPUTED,
    _LAST_SYNTH_MODEL,
    _premise_verdict_of_last_call,
    _retain_task_result,
    _set_request_overrides,
    _synth_telemetry,
)
from server.memory.domain import ask_attribution as _attr


def _world_knowledge_allowed(world_knowledge: str | None, settings: Any) -> bool:
    """Whether the synthesizer may bridge with general knowledge."""
    _settings = settings
    # General-knowledge bridging: the request's mode, else the
    # deployment's; anything but "allow" keeps the synthesizer memory-only.
    _wk_mode = world_knowledge or _settings.memory_ask_world_knowledge
    return str(_wk_mode or "forbid").lower() == "allow"


def _pin_identity(search_input: Any, precedent_key: str) -> tuple[Any, str]:
    """The ask's input with the token's read scope pinned, and the precedent
    key to use. memory.search pins the scope inside its own handler, but the
    pipeline's completion, injection and mask reads build their statements
    from `search_input` directly, so without the pin a token bound to one end
    user reads every user's rows through them. A pin that changed the
    identity also drops precedent reuse: the handler hashed the precedent
    shape from the body, which did not name the pinned user."""
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import pin_read_filters

    pinned, changed = pin_read_filters(search_input, current_auth())
    return pinned, "" if changed else precedent_key


async def _fast_precedent(
    search_input: Any, precedent_key: str, precedent_shape: str, settings: Any
) -> MemoryAskResult | None:
    """The stored answer for this question+shape when nothing it read has
    moved since; None sends the ask on to retrieval."""
    _settings = settings
    # Fast precedent path: when no plane this namespace reads
    # has been written since the stored answer's window was retrieved, the
    # window IS that window and retrieval is skipped. Misses fall through to
    # retrieval and the byte-identical precedent check below.
    if precedent_key:
        from server.memory.app import precedents as _prec_fast

        _prior = await _prec_fast.fast_reuse(
            precedent_key,
            org=getattr(search_input, "org", "") or "",
            namespace=getattr(search_input, "namespace", "") or "",
            shape_sha256=precedent_shape,
            max_age_hours=int(_settings.memory_ask_precedents_fast_max_age_hours),
        )
        if _prior is not None:
            return MemoryAskResult(
                answer=_prior.answer,
                source="precedent",
                memory_keys=list(_prior.cited_keys),
                results=[],
                synth_model=_prior.synth_model,
                route_tier="",
                route_reason=f"precedent:unmoved:reused={_prior.reuse_count + 1}",
                depth="excerpt",
            )
    return None


async def _reuse_precedent(
    run: _AskRun, precedent_key: str, precedent_shape: str
) -> MemoryAskResult | None:
    """The stored answer written from this exact window, served
    without a model call; None when there is none."""
    search_input, _settings = run.search_input, run.settings
    # Precedent reuse: the window the synthesizer is about to
    # read is final here. If an earlier answer to this question+shape was
    # written from the SAME keys with the SAME content hashes, the model
    # would read the same bytes — serve that answer and skip the call.
    if precedent_key:
        from server.memory.app import precedents as _prec

        prior = await _prec.reuse(
            precedent_key,
            org=getattr(search_input, "org", "") or "",
            namespace=getattr(search_input, "namespace", "") or "",
            window=list(run.memories),
            shape_sha256=precedent_shape,
            max_age_hours=int(_settings.memory_ask_precedents_max_age_hours),
            retrieved_at=run.retrieved_at,
        )
        if prior is not None:
            _shown_keys = _keys_of(run.memories)
            return MemoryAskResult(
                answer=prior.answer,
                source="precedent",
                memory_keys=[k for k in prior.cited_keys if k in _shown_keys] or _shown_keys,
                results=run.results,
                embedding_model=run.model,
                synth_model=prior.synth_model,
                route_tier="",
                route_reason=f"precedent:reused={prior.reuse_count + 1}",
                facets=run.facets,
                facet_memories_added=run.facet_added,
                depth="excerpt",
                context_tokens_est=run.context_tokens_est,
                context_trimmed=run.context_trimmed,
            )
    return None


def _synthesized_result(
    run: _AskRun,
    reader: _AskReader,
    ans: _AskAnswer,
    *,
    verdict: Any,
    note: str,
    premise: Any,
    premise_text: str,
    deferred_run: Any,
    knowledge_hits: int,
) -> MemoryAskResult:
    """The synthesized answer with every telemetry field the phases left."""
    _pool_facts, _pool_chunks = pool_composition(run.memories)
    return MemoryAskResult(
        answer=ans.answer,
        source="synthesized",
        pool_facts=_pool_facts,
        pool_chunks=_pool_chunks,
        when_evidence_turns=ans.when_turns_used,
        memory_keys=ans.cited,
        results=run.results,
        embedding_model=run.model,
        synth_model=_LAST_SYNTH_MODEL.get() or str(getattr(ans.client, "model", "") or ""),
        route_tier=ans.tier,
        route_reason=ans.reason,
        escalated=ans.escalated,
        consistency=verdict.label,
        consistency_score=verdict.score,
        consistency_note=note,
        contradicted_key=verdict.contradicted_key if verdict.contradicted else "",
        deferred_consistency=deferred_run,
        premise_conflict=premise.memory_key if premise.conflict else "",
        premise_note=premise_text,
        premise_verdict=_premise_verdict_of_last_call(),
        attribution_note=str(_attr.LAST_ATTRIBUTION.get() or ""),
        identifier_chase_hops=run.chase_hops_done,
        identifier_chase_added=len(run.chase_added),
        facets=run.facets,
        facet_memories_added=run.facet_added,
        gate_mode=run.gate_mode,
        gate_kept=run.gate_kept,
        gate_dropped=run.gate_dropped,
        depth=ans.answered_depth,
        deep_fired=ans.deep_fired,
        deep_reason=ans.deep_reason,
        deep_selector=ans.deep_selector_used,
        deep_first=ans.deep_first_state,
        **_synth_telemetry(),
        deep_keys=ans.deep_keys,
        deep_chars=ans.deep_chars,
        code_hits=sum(
            1 for _h in run.synth_pool if str(getattr(_h, "source", "") or "") == "code_index"
        ),
        timeline_rows=run.timeline_rows,
        context_tokens_est=run.context_tokens_est,
        context_trimmed=run.context_trimmed,
        summary_hits=run.summary_hits,
        knowledge_hits=knowledge_hits,
        count_recomputed=bool(_LAST_COUNT_RECOMPUTED.get()),
        decomposed=list(run.decomposed),
        superseded_dropped=run.superseded_dropped,
        stale_sentences_masked=run.stale_sentences_masked,
        entity_followed=list(run.entity_followed),
        decompose_failed=int(run.decomp_stats.get("failed", 0)),
        pool_k=run.pool_k_used,
        grounding=answer_grounding(ans.answer, allow_world=reader.world_knowledge),
        pending_evidence=int(run.pending_evidence_n),
    )


def _presupposition_on(presupposition_rule: bool | None) -> bool:
    return True if presupposition_rule is None else bool(presupposition_rule)


def _start_knowledge_read(
    search_input: Any, question: str | None, knowledge: bool | None
) -> asyncio.Task[tuple[str, int]]:
    """The knowledge plane's lookup (its own query embedding and a hybrid
    read) depends only on the request, so it starts here and is awaited right
    before synthesis; memory retrieval runs meanwhile. `knowledge_passages` is
    fail-open inside, so the task never raises."""
    task: asyncio.Task[tuple[str, int]] = create_deferred_task(
        knowledge_passages(search_input, question or search_input.query, enabled=knowledge)
    )
    task.add_done_callback(_retain_task_result)
    return task


def _knowledge_not_needed(task: asyncio.Task[tuple[str, int]]) -> None:
    """Cancel the knowledge read if it is still running, so its ClickHouse read
    does not outlive the ask: an ask that returns early (abstained, record,
    retrieval-only, capped, no client, precedent) or raises never reads the
    block, and a read left running lands in whatever client the loop serves
    next — under the unit suite's session loop, the next test's fake."""
    if not task.done():
        task.cancel()


async def _fill_pool(
    run: Any, *, llm: Any, llm_cheap: Any, decompose: str | None
) -> tuple[bool, bool, bool]:
    """Retrieval, the question-shaped legs and the completions that add rows,
    then the hard tag belt after the last of them; sets the run's results and
    synthesis pool. Returns the reader notes the question's shape switched on
    (inference, temporal derive, chain)."""
    await _retrieve(run)
    notes = await _widen_by_question(run, llm=llm, llm_cheap=llm_cheap, decompose=decompose)
    await _order_pool(run)

    await _inject_named_evidence(run)
    await _complete_options(run)

    await _complete_bridges(run)

    await _complete_facets(run)

    # The hard tag belt and the authority's check, after the last leg that adds a row.
    run.ranked = await _tag_belt(run.ranked, run.search_input)
    run.ranked = await _served_pool(run.ranked, run.search_input)
    run.results = run.ranked[: run.target_k]
    run.synth_pool = run.ranked[: run.window]
    return notes


async def ask(
    search_input: Any,
    *,
    llm: Any,
    question: str | None = None,
    allow_synthesis: bool = True,
    budget_capped: bool = False,
    max_answer_chars: int = 0,
    llm_cheap: Any = None,
    llm_mid: Any = None,
    mid_routes: str | None = None,
    budget_ratio: float = 0.0,
    depth: str = "auto",
    deep_select: str | None = None,
    deep_first: bool | None = None,
    recount: bool | None = None,
    token_budget: int | None = None,
    personalize: bool | None = None,
    window_rel_floor: float | None = None,
    decompose: str | None = None,
    gh_items_fallback: bool | None = None,
    precedent_key: str = "",
    precedent_shape: str = "",
    scratchpad: str = "",
    world_knowledge: str | None = None,
    recent_messages: list[dict[str, Any]] | None = None,
    presupposition_rule: bool | None = None,
    set_window: int | None = None,
    knowledge: bool | None = None,
    consistency_mode: str | None = None,
    route_prior: str | None = None,
    route_prior_max_cheap: float | None = None,
    default_answer_chars: int | None = None,
) -> MemoryAskResult:
    """Retrieve then optionally synthesize. `search_input` is a ready
    MemorySearchInput; `llm`, `allow_synthesis` and `budget_capped` are decided
    by the handler (which owns identity + cost-cap plumbing), exactly as
    handle_code_ask decides them for code.app.answers.ask.

    `llm` is the STRONG synthesis client. When `llm_cheap` is also given the
    two-tier cascade is on: memory.domain.synth_policy picks a tier from the
    retrieval signals (and `budget_ratio`, the namespace's 24h spend / cap),
    and a cheap answer that fails the citation contract is escalated to `llm`
    once. The decision rides the result as route_tier / route_reason /
    synth_model / escalated. `llm_mid` is a third, MID tier:
    the strong-routed asks whose reason is in `mid_routes` (the request's
    pin, else the setting) are answered by it and escalated the same way.

    `depth` is the caller's reading depth: "excerpt" answers
    from fact excerpts only; "raw" also opens the source turns behind the
    selected facts on every call; "auto" opens them for a temporal question.
    The pass is memory.app.deep_evidence; it escalates the evidence, the
    cascade above escalates the model, and both can run on one ask.

    `precedent_key` is the handler's digest of the normalised
    question and the request shape; non-empty = precedent reuse is on for
    this ask. Right before synthesis the stored answer for that key is
    served, without an LLM call, if the synthesis window is byte-identical
    to the evidence it was written from; after a fresh answer that cites,
    the answer is stored as the new precedent."""
    recent_block = recent_turns_block(recent_messages or [])
    search_input, precedent_key = _pin_identity(search_input, precedent_key)
    # An advice request presupposes no recorded event ("would it be a good idea to
    # attend my reunion?"): its answer is the advice, never a note that the
    # memories hold no reunion.
    from server.memory.app.ask_when_evidence import is_advice_request

    presupposition = _presupposition_on(presupposition_rule) and not is_advice_request(
        question or ""
    )
    _knowledge_task = _start_knowledge_read(search_input, question, knowledge)

    # Every exit below — an early answer, a precedent, an error — leaves the
    # knowledge read cancelled; after a normal await it is already done.
    try:
        # `question` is the text the model answers; search_input.query is what
        # retrieval embedded (the stem, when the caller's query carried an answer
        # scaffold). Same string on the plain path.
        question = question or search_input.query
        _set_request_overrides(recount, personalize)
        from server.settings import settings as _settings

        run = _ask_pool_size(
            search_input,
            question,
            set_window=set_window,
            settings=_settings,
        )
        _prior_result = await _fast_precedent(
            run.search_input, precedent_key, precedent_shape, _settings
        )
        if _prior_result is not None:
            return _prior_result
        inference_note, temporal_derive, chain_note = await _fill_pool(
            run, llm=llm, llm_cheap=llm_cheap, decompose=decompose
        )
        _wk_allow = _world_knowledge_allowed(world_knowledge, _settings)

        early, hedge_ident = await _early_answer(
            run,
            allow_synthesis=allow_synthesis,
            budget_capped=budget_capped,
            llm=llm,
            gh_items_fallback=gh_items_fallback,
        )
        if early is not None:
            return early

        await _ground_and_cut(run, window_rel_floor)

        # ---- grounded synthesis --------------------------------------------------
        record_note = await _settle_window(run, token_budget=token_budget)
        prior_result = await _reuse_precedent(run, precedent_key, precedent_shape)
        if prior_result is not None:
            return prior_result
        timeline_text, temporal_question, as_of = _read_time(run)
        omitted = _omitted_rows(run)

        answer_chars = _answer_chars(
            question,
            run.intent,
            max_answer_chars=max_answer_chars,
            default_answer_chars=default_answer_chars,
            settings=_settings,
        )

        tier, reason, client, stage = _route_tier(
            run,
            llm=llm,
            llm_cheap=llm_cheap,
            llm_mid=llm_mid,
            hedge_ident=hedge_ident,
            as_of=as_of,
            budget_ratio=budget_ratio,
            route_prior=route_prior,
            route_prior_max_cheap=route_prior_max_cheap,
            mid_routes=mid_routes,
        )

        premise, premise_system = await _check_premise(run, llm=llm, llm_cheap=llm_cheap)

        knowledge_block, knowledge_hits = await _knowledge_task
        reader = _AskReader(
            question=question,
            answer_chars=answer_chars,
            hedge_ident=hedge_ident,
            as_of=as_of,
            stage=stage,
            recent_block=recent_block,
            presupposition=presupposition,
            record_note=record_note,
            inference_note=inference_note,
            temporal_derive=temporal_derive,
            chain_note=chain_note,
            knowledge_block=knowledge_block,
            intent=run.intent,
            omitted=omitted,
            timeline=timeline_text,
            premise_system=premise_system,
            world_knowledge=_wk_allow,
            scratchpad=scratchpad,
        )
        ans = _AskAnswer(tier=tier, reason=reason, client=client)
        await _answer_with_deep_pass(
            run,
            reader,
            ans,
            llm=llm,
            llm_cheap=llm_cheap,
            depth=depth,
            temporal_question=temporal_question,
            deep_select=deep_select,
            deep_first=deep_first,
        )

        await _mid_verify(run, reader, ans, llm=llm)

        verdict, note, premise_text, _deferred_run = await _audit_answer(
            run,
            ans,
            llm=llm,
            llm_cheap=llm_cheap,
            consistency_mode=consistency_mode,
            premise=premise,
            precedent_key=precedent_key,
            precedent_shape=precedent_shape,
        )
        return _synthesized_result(
            run,
            reader,
            ans,
            verdict=verdict,
            note=note,
            premise=premise,
            premise_text=premise_text,
            deferred_run=_deferred_run,
            knowledge_hits=knowledge_hits,
        )
    finally:
        _knowledge_not_needed(_knowledge_task)
