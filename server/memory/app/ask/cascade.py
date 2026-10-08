"""The synthesis cascade: tier routing, the first answer and its escalation,
the deep-evidence pass, the mid-tier verifier and the answer audit with the
precedent store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from server.core.background import create_deferred_task
from server.memory.app.ask.identifiers import _technical_identifiers
from server.memory.app.ask.pool import _AskRun
from server.memory.app.ask.reader import _AskReader
from server.memory.app.ask.result import _by_keys, _keys_cited, _keys_of
from server.memory.app.ask.retrieval_legs import _order_window_turns
from server.memory.app.ask.state import _LAST_SYNTH_MODEL, _retain_task_result
from server.memory.app.ask.synthesis import _not_recorded_second, _synthesize


@dataclass(slots=True)
class _AskAnswer:
    """The answer as the synthesis cascade revises it: the tier and client
    that wrote it, what it cites, and how the deep-evidence pass ran."""

    tier: str
    reason: str
    client: Any
    escalated: bool = False
    answer: str = ""
    cited: list[str] = dc_field(default_factory=list)
    first_skipped: bool = False  # the deep-first order served no excerpt answer
    when_turns_used: int = 0  # the not-recorded second reading
    # The pre-started deep selection and the client it runs on.
    select_client: Any = None
    deep_task: asyncio.Task[Any] | None = None
    deep_task_key: tuple[str, str] | None = None
    deep_first_on: bool = False
    deep_first_state: str = ""
    # The deep-evidence pass, as it ran.
    answered_depth: str = "excerpt"
    deep_fired: bool = False
    deep_reason: str = ""
    deep_keys: list[str] = dc_field(default_factory=list)
    deep_chars: int = 0
    deep_selector_used: str = ""
    deep_block_served: str = ""
    deep_extra: list[Any] = dc_field(default_factory=list)


def _route_tier(
    run: _AskRun,
    *,
    llm: Any,
    llm_cheap: Any,
    llm_mid: Any,
    hedge_ident: str,
    as_of: Any,
    budget_ratio: float,
    route_prior: str | None,
    route_prior_max_cheap: float | None,
    mid_routes: str | None,
) -> tuple[str, str, Any, str]:
    """Pick the synthesis tier from the retrieval signals (services.
    synth_policy). Returns (tier, reason, client, stage)."""
    _settings = run.settings
    # Tier routing. Without a cheap client this is the single-model path,
    # labelled so the audit trail can tell it apart.
    from server.memory.domain import synth_policy as _policy

    tier, reason = _policy.STRONG, "single_tier"
    client = llm
    stage = "ask_synthesize"
    if llm_cheap is not None or llm_mid is not None:
        _sims = [float(getattr(m, "similarity", 0.0) or 0.0) for m in run.memories[:2]]
        _top = _sims[0] if _sims else 0.0
        _second = _sims[1] if len(_sims) > 1 else 0.0
        decision = _policy.decide(
            _policy.SynthFeatures(
                query_chars=len(run.query_text),
                n_memories=len(run.memories),
                top_similarity=_top,
                similarity_margin=max(0.0, _top - _second),
                retrieval_confidence=float(
                    getattr(run.retrieval, "retrieval_confidence", 0.0) or 0.0
                ),
                retrieval_risk=str(getattr(run.retrieval, "retrieval_risk", "") or ""),
                route_agreement=float(getattr(run.retrieval, "route_agreement", 0.0) or 0.0),
                hedged=bool(hedge_ident),
                has_identifier=bool(run.idents) or bool(_technical_identifiers(run.query_text)),
                evidence_tokens=sum(
                    len(str(getattr(m, "content_excerpt", "") or "")) for m in run.memories
                )
                // 4,
                as_of=as_of is not None,
                budget_ratio=float(budget_ratio or 0.0),
                query=run.query_text,
            ),
            budget_cheap_ratio=float(_settings.memory_ask_synth_budget_cheap_ratio),
            max_cheap_evidence_tokens=int(_settings.memory_ask_synth_cheap_max_evidence_tokens),
            # The verdict prior, pinned by the request or the setting.
            route_prior=_policy.parse_route_prior(
                route_prior
                if route_prior is not None
                else str(_settings.memory_ask_synth_route_prior)
            ),
            max_cheap_prior=float(
                route_prior_max_cheap
                if route_prior_max_cheap is not None
                else _settings.memory_ask_synth_route_prior_max_cheap
            ),
            # The mid tier takes the named strong routes when a
            # mid client exists (the request's pin over the setting).
            mid_routes=(
                _policy.parse_mid_routes(
                    mid_routes
                    if mid_routes is not None
                    else str(_settings.memory_ask_synth_mid_routes)
                )
                if llm_mid is not None
                else None
            ),
        )
        tier, reason = decision.tier, decision.reason
        if decision.tier == _policy.MID:
            client = llm_mid
        elif decision.cheap and llm_cheap is None:
            # A mid tier without a cheap one: the rules' cheap route has no
            # client, so the ask stays on the strong tier and says why.
            tier, reason, client = _policy.STRONG, f"{reason}:no_cheap_tier", llm
        else:
            client = llm_cheap if decision.cheap else llm
        stage = f"ask_synthesize_{tier}"
    return tier, reason, client, stage


async def _prepare_synthesis(
    run: _AskRun,
    reader: _AskReader,
    ans: _AskAnswer,
    *,
    depth: str,
    temporal_question: bool,
    llm_cheap: Any,
    deep_select: str | None,
    deep_first: bool | None,
) -> None:
    """Before the first synthesis: start the deep-evidence selection whose
    trigger is known up front and read the ordering question's window turns."""
    from server.memory.domain import synth_policy as _policy

    search_input, question, _settings = run.search_input, run.question, run.settings
    # A deep-evidence pass whose trigger is known before the
    # first synthesis — depth="raw", or a temporal question —
    # selects its evidence while that synthesis runs. The selection reads the
    # ranked pool and the question, never the answer, so the result is the
    # one the serial order produced. Skipped when the cheap tier answers
    # first: the cascade may hand the deep pass an escalated client.
    ans.deep_task = None
    ans.deep_task_key = None
    _deep_upfront = (
        "requested"
        if str(depth or "auto") == "raw"
        else ("temporal" if str(depth or "auto") == "auto" and temporal_question else "")
    )
    # The selector runs on its own tier (the cheap client when
    # the cascade has one), not on whichever client synthesizes.
    ans.select_client = _deep_select_client(ans.client, llm_cheap, _settings)
    # With the trigger known up front the excerpt synthesis is
    # a third model call whose answer was served on 0.4% of such asks. Await
    # the selection and synthesize once with the raw turns; the excerpt
    # synthesis runs only when the block is empty or the deep answer fails the
    # citation contract (then the deep pass's keep-or-replace rule decides).
    ans.deep_first_on = False
    ans.deep_first_state = ""
    if _deep_upfront and ans.tier != _policy.CHEAP:
        ans.deep_task_key = (
            _deep_upfront,
            _deep_selector_for(_deep_upfront, deep_select, _settings),
        )
        ans.deep_task = create_deferred_task(
            _run_deep_pass(
                ans.select_client,
                search_input,
                run.ranked,
                run.memories,
                trigger=ans.deep_task_key[0],
                selector=ans.deep_task_key[1],
                settings=_settings,
            )
        )
        ans.deep_task.add_done_callback(_retain_task_result)
        ans.deep_first_on = True if deep_first is None else bool(deep_first)

    reader.order_turns_text, reader.order_turns_note = await _order_window_turns(
        search_input, question, _settings
    )


async def _first_answer(run: _AskRun, reader: _AskReader, ans: _AskAnswer, *, llm: Any) -> None:
    """The first synthesis over the excerpt window (skipped when the
    deep-first order already holds a raw block), the not-recorded second
    reading, and the cascade verifier that escalates a
    failed cheap or mid answer to the strong client once."""
    from server.core import request_work as _rw
    from server.memory.domain import synth_policy as _policy

    search_input, question, _settings = run.search_input, run.question, run.settings
    ans.first_skipped = False
    if ans.deep_first_on and ans.deep_task is not None:
        try:
            _deep_early = await ans.deep_task
        except Exception:  # noqa: BLE001 — the pass is fail-open; so is the order
            _deep_early = None
        if _deep_early is not None and getattr(_deep_early, "block", None):
            ans.first_skipped = True
        else:
            ans.deep_first_state = "empty_block"
    _rw.mark_stage("window")
    if ans.first_skipped:
        ans.answer, ans.cited = "", []
    else:
        ans.answer = await _synthesize(
            ans.client, question, run.memories, reader.answer_chars, **reader.first_kwargs()
        )
        # Cite what the answer USED, not everything it saw — its [key] markers.
        ans.cited = _keys_cited(ans.answer, run.memories)

    # An answer that says the memories "do not record" the
    # asked fact is asked again over the dated raw turns that carry it.
    ans.when_turns_used = 0
    if not ans.first_skipped:
        second, _nr_n = await _not_recorded_second(
            ans.client,
            question,
            run.memories,
            reader.answer_chars,
            reader.first_kwargs(),
            search_input=search_input,
            answer=ans.answer,
        )
        if second.strip():
            ans.answer, ans.cited, ans.when_turns_used = (
                second,
                _keys_cited(second, run.memories),
                _nr_n,
            )

    # Cascade verifier: a cheap (or mid) answer that is empty,
    # cites nothing, or hedges did not do the job — pay for the strong model
    # once. Under budget pressure the cheap answer stands (the alternative is
    # retrieval_only). Under the deep-first order the mid tier's first
    # answer is the deep answer, so its verifier runs after the deep pass.
    if (
        ans.tier in (_policy.CHEAP, _policy.MID)
        and ans.reason != "budget_pressure"
        and not ans.first_skipped
    ):
        failed = _policy.cheap_answer_failed(ans.answer, ans.cited, tier=ans.tier)
        if failed:
            ans.answer = await _synthesize(
                llm,
                question,
                run.memories,
                reader.answer_chars,
                hedge_ident=reader.hedge_ident,
                as_of=reader.as_of,
                stage="ask_synthesize_strong",
                recent_block=reader.recent_block,
                presupposition=reader.presupposition,
                record_note=reader.record_note,
                inference_note=reader.inference_note,
                temporal_derive=reader.temporal_derive,
                chain_note=reader.chain_note,
                knowledge_block=reader.knowledge_block,
                intent=run.intent,
                omitted=reader.omitted,
                timeline=reader.timeline,
                premise_system=reader.premise_system,
                world_knowledge=reader.world_knowledge,
            )
            ans.cited = _keys_cited(ans.answer, run.memories)
            ans.tier, ans.reason, ans.escalated, ans.client = (
                _policy.STRONG,
                f"{ans.reason}:{failed}",
                True,
                llm,
            )


def _deep_trigger(ans: _AskAnswer, *, depth: str, temporal_question: bool) -> str:
    """Why the deep-evidence pass runs, or "" when it does not: the caller's
    depth="raw" or a temporal question. Resets the pass's telemetry on the
    answer record first."""
    # ---- deep-evidence pass ---------------------------------
    # Runs BEFORE the answer-vs-citation audit below so the audited answer is
    # the one returned.
    # The cascade above escalates the MODEL over the same excerpts. This
    # escalates the EVIDENCE: on depth="raw" or a temporal question, one
    # selection call picks the facts
    # worth opening, their source turns are read through memory_belief_sources
    # (bounded; scoped rows first), and synthesis runs once more
    # with the raw turns appended. Fail-open: an empty block keeps the answer.
    ans.answered_depth = "excerpt"
    ans.deep_fired = False
    ans.deep_reason = ""
    ans.deep_keys = []
    ans.deep_chars = 0
    depth_mode = str(depth or "auto")
    deep_trigger = ""
    ans.deep_selector_used = ""
    ans.deep_block_served = ""
    ans.deep_extra = []
    if depth_mode == "raw":
        deep_trigger = "requested"
    elif depth_mode == "auto" and temporal_question:
        deep_trigger = "temporal"
    return deep_trigger


async def _deep_answer(
    run: _AskRun,
    reader: _AskReader,
    ans: _AskAnswer,
    *,
    depth: str,
    temporal_question: bool,
    deep_select: str | None,
) -> None:
    """The deep-evidence pass: on depth="raw" or a temporal question, open the
    selected facts' source turns and synthesize once more over them, keeping
    the deeper answer unless it is strictly worse."""
    from server.memory.domain import synth_policy as _policy

    search_input, question, _settings = run.search_input, run.question, run.settings
    deep_trigger = _deep_trigger(ans, depth=depth, temporal_question=temporal_question)
    if deep_trigger:
        pool_n = int(_settings.memory_ask_deep_evidence_pool)
        deep_selector = _deep_selector_for(deep_trigger, deep_select, _settings)
        if ans.deep_task is not None and ans.deep_task_key == (deep_trigger, deep_selector):
            deep = await ans.deep_task
        else:
            if ans.deep_task is not None:
                ans.deep_task.cancel()
            deep = await _run_deep_pass(
                ans.select_client,
                search_input,
                run.ranked,
                run.memories,
                trigger=deep_trigger,
                selector=deep_selector,
                settings=_settings,
            )
        ans.deep_fired = True
        ans.deep_reason = deep_trigger
        ans.deep_selector_used = deep_selector
        ans.deep_keys = list(deep.selected_keys)
        ans.deep_chars = int(deep.chars)
        if deep.block:
            # Make sure every opened memory is also in the excerpt list so its
            # [memory_key] marker is citable even when it ranked past the window.
            shown_keys = {getattr(m, "memory_key", "") for m in run.memories}
            extra = [
                m
                for m in run.ranked[:pool_n]
                if getattr(m, "memory_key", "") in deep.selected_keys
                and getattr(m, "memory_key", "") not in shown_keys
            ]
            # The reader answers the caller's QUESTION — the scaffolded text
            # with its option block — never the retrieval stem: with the stem,
            # the deep pass answers without the options, no option program can
            # read a letter, and the letterless answer replaces the first
            # pass's (measured: 5/15 → 1/15 on a multiple-choice slice, every answer
            # right in substance and none carrying a letter).
            deep_answer = await _synthesize(
                ans.client,
                question,
                run.memories + extra,
                reader.answer_chars,
                hedge_ident=reader.hedge_ident,
                as_of=reader.as_of,
                stage="ask_synthesize_deep",
                recent_block=reader.recent_block,
                presupposition=reader.presupposition,
                record_note=reader.record_note,
                inference_note=reader.inference_note,
                temporal_derive=reader.temporal_derive,
                chain_note=reader.chain_note,
                knowledge_block=reader.knowledge_block,
                intent=run.intent,
                omitted=reader.omitted,
                timeline=reader.timeline,
                raw_evidence=deep.block,
                world_knowledge=reader.world_knowledge,
            )
            deep_cited = _keys_cited(deep_answer, run.memories + extra)
            ans.deep_block_served, ans.deep_extra = deep.block, extra
            if ans.first_skipped:
                # The excerpt answer exists only when the rule
                # below needs it — when the deep answer is empty or fails the
                # citation contract. A deep answer that passes is served as
                # it would have been, without the call it never needed.
                if deep_answer.strip() and not _policy.cheap_answer_failed(deep_answer, deep_cited):
                    ans.deep_first_state = "single"
                else:
                    ans.deep_first_state = "deep_failed"
                    ans.answer = await _synthesize(
                        ans.client,
                        question,
                        run.memories,
                        reader.answer_chars,
                        **reader.first_kwargs(),
                    )
                    ans.cited = _keys_cited(ans.answer, run.memories)
            # Keep the deeper answer unless it is strictly worse: empty, or it
            # hedges where the excerpt answer did not.
            if deep_answer.strip() and not (
                _policy.cheap_answer_failed(deep_answer, deep_cited)
                and not _policy.cheap_answer_failed(ans.answer, ans.cited)
            ):
                ans.answer, ans.cited = deep_answer, deep_cited
                ans.answered_depth = "raw"
            if ans.first_skipped and not ans.when_turns_used:
                # The deep-first order served the deep
                # answer without the first synthesis, and the not-recorded second
                # reading above never ran; it runs here on the answer to be served.
                second, _nr_n = await _not_recorded_second(
                    ans.client,
                    question,
                    run.memories,
                    reader.answer_chars,
                    reader.first_kwargs(),
                    search_input=search_input,
                    answer=ans.answer,
                )
                if second.strip():
                    ans.answer, ans.cited = second, _keys_cited(second, run.memories)
                    ans.when_turns_used = _nr_n
        elif ans.first_skipped:
            # The selection produced nothing to open after all (a read that
            # returned no turns): the excerpt synthesis is the answer.
            ans.deep_first_state = "empty_block"
            ans.answer = await _synthesize(
                ans.client, question, run.memories, reader.answer_chars, **reader.first_kwargs()
            )
            ans.cited = _keys_cited(ans.answer, run.memories)


async def _answer_with_deep_pass(
    run: _AskRun,
    reader: _AskReader,
    ans: _AskAnswer,
    *,
    llm: Any,
    llm_cheap: Any,
    depth: str,
    temporal_question: bool,
    deep_select: str | None,
    deep_first: bool | None,
) -> None:
    """The first answer and the deep-evidence pass, with the up-front deep
    selection `_prepare_synthesis` may start cancelled on every exit: when
    the first answer raises, nothing awaits the task and its model call would
    run on after the ask has failed. Once `_deep_answer` returns nothing
    reads the task, so on the normal path the cancel is a no-op."""
    try:
        await _prepare_synthesis(
            run,
            reader,
            ans,
            depth=depth,
            temporal_question=temporal_question,
            llm_cheap=llm_cheap,
            deep_select=deep_select,
            deep_first=deep_first,
        )
        await _first_answer(run, reader, ans, llm=llm)
        await _deep_answer(
            run,
            reader,
            ans,
            depth=depth,
            temporal_question=temporal_question,
            deep_select=deep_select,
        )
    finally:
        if ans.deep_task is not None and not ans.deep_task.done():
            ans.deep_task.cancel()


async def _mid_verify(run: _AskRun, reader: _AskReader, ans: _AskAnswer, *, llm: Any) -> None:
    """Under the deep-first order the mid tier's answer is
    checked once it is final, and a failed one escalates to the strong
    client over the same raw block."""
    from server.memory.domain import synth_policy as _policy

    question = run.question
    # The mid tier's verifier under the deep-first order — the
    # answer it checks is the one about to be served (the deep answer, or the
    # excerpt answer the deep pass's rule kept). A failed one escalates to the strong
    # tier once, over the same raw block when there was one.
    if ans.tier == _policy.MID and ans.first_skipped and ans.reason != "budget_pressure":
        failed = _policy.cheap_answer_failed(ans.answer, ans.cited, tier=ans.tier)
        if failed:
            _esc_kwargs = reader.first_kwargs()
            _esc_kwargs["stage"] = "ask_synthesize_strong"
            if ans.deep_block_served:
                _esc_kwargs["raw_evidence"] = ans.deep_block_served
            ans.answer = await _synthesize(
                llm, question, run.memories + ans.deep_extra, reader.answer_chars, **_esc_kwargs
            )
            ans.cited = _keys_cited(ans.answer, run.memories + ans.deep_extra)
            ans.tier, ans.reason, ans.escalated, ans.client = (
                _policy.STRONG,
                f"{ans.reason}:{failed}",
                True,
                llm,
            )
            if ans.deep_block_served:
                ans.answered_depth = "raw"


async def _audit_answer(
    run: _AskRun,
    ans: _AskAnswer,
    *,
    llm: Any,
    llm_cheap: Any,
    consistency_mode: str | None,
    premise: Any,
    precedent_key: str,
    precedent_shape: str,
) -> tuple[Any, str, str, Any]:
    """After the answer is final: the answer-vs-citation judge (inline or
    deferred), the citation fallback, the premise and contradiction notes,
    and the precedent store. Returns (verdict,
    consistency note, premise note, the deferred judge run or None)."""
    from server.core import request_work as _rw
    from server.memory.domain import synth_policy as _policy

    search_input, question, _settings = run.search_input, run.question, run.settings
    # Answer-vs-citation audit: a fluent, cited answer can
    # still say the opposite of its source, and the citation verifier above
    # cannot see that. One judge call (the cheap client when the cascade is
    # on) labels the answer; the verdict is reported, never acted on.
    from server.memory.app import answer_consistency as _ac

    _rw.mark_stage("synthesis")
    verdict = _ac.Verdict()
    _deferred_judge: Any = None
    _cons_mode = str(consistency_mode or _settings.memory_ask_consistency_mode)
    if bool(_settings.memory_ask_consistency_check) and _cons_mode == "deferred":
        # The judge is the last serial step before the response
        # (691 ms p50, on every synthesized ask) and changes
        # nothing the caller receives but the label.
        # Answer now; the handler runs this after the response and lands the
        # verdict on the ledger and the usage funnel instead of the wire.
        _judge, _q, _a, _rows = (
            llm_cheap if llm_cheap is not None else llm,
            question,
            ans.answer,
            _by_keys(run.memories, ans.cited),
        )

        async def _deferred_judge() -> Any:
            return await _ac.check(_judge, _q, _a, _rows)

    elif bool(_settings.memory_ask_consistency_check):
        judge = llm_cheap if llm_cheap is not None else llm
        verdict = await _ac.check(judge, question, ans.answer, _by_keys(run.memories, ans.cited))

    # Fall back to citing all grounding memories when the model emitted no
    # parseable markers.
    _rw.mark_stage("judge")
    marked_cited = bool(ans.cited)
    ans.cited = ans.cited or _keys_of(run.memories)
    note = ""
    premise_text = ""
    if premise.conflict:
        premise_text = (
            f"question presupposes: {premise.premise}"
            if premise.premise
            else "question premise contradicted"
        )
        if premise.correction:
            premise_text = f"{premise_text}; [{premise.memory_key}] records: {premise.correction}"
    if verdict.contradicted:
        note = (
            f"contradicts [{verdict.contradicted_key}]"
            if verdict.contradicted_key
            else "contradicts a cited memory"
        )
        if verdict.span:
            note = f"{note}: {verdict.span}"
    # A fresh answer becomes the precedent for its question+shape when it
    # passed the citation contract on the excerpt window: cited, not hedged,
    # not contradicted by its own evidence, no raw-turn evidence outside the
    # window. The write hashes the window off the request path.
    _precedent_eligible = bool(
        precedent_key
        and marked_cited
        and not ans.deep_fired
        and not _policy.cheap_answer_failed(ans.answer, ans.cited)
    )
    _prec_synth_model = _LAST_SYNTH_MODEL.get() or str(getattr(ans.client, "model", "") or "")
    _prec_cited, _prec_answer = list(ans.cited), ans.answer

    def _store_precedent() -> None:
        from server.memory.app import precedents as _prec

        _prec.schedule_store(
            precedent_key,
            org=getattr(search_input, "org", "") or "",
            namespace=getattr(search_input, "namespace", "") or "",
            question=question or "",
            shape_sha256=precedent_shape,
            answer=_prec_answer,
            cited_keys=_prec_cited,
            window=list(run.memories),
            synth_model=_prec_synth_model,
            retrieved_at=run.retrieved_at,
        )

    _deferred_run: Any = None
    if _deferred_judge is not None:
        # The verdict gates the precedent (a contradicted answer is no
        # precedent), so under the deferred mode the store waits for it too.
        _inner = _deferred_judge

        async def _deferred_run() -> Any:
            v = await _inner()
            if _precedent_eligible and not v.contradicted:
                _store_precedent()
            return v

    elif _precedent_eligible and not verdict.contradicted:
        _store_precedent()
    return verdict, note, premise_text, _deferred_run


def _deep_selector_for(trigger: str, deep_select: str | None, settings: Any) -> str:
    """Which selector the deep pass uses: the caller's `deep_select`, else
    the temporal trigger's setting, else the general
    `memory_ask_deep_evidence_select`; anything but llm/top is llm."""
    selector = str(
        deep_select
        or (str(settings.memory_ask_temporal_deep_select) if trigger == "temporal" else "")
        or settings.memory_ask_deep_evidence_select
        or "llm"
    )
    return selector if selector in ("llm", "top") else "llm"


def _deep_select_client(client: Any, llm_cheap: Any, settings: Any) -> Any:
    """Which client runs the deep selector: the cascade's
    cheap client under `memory_ask_deep_select_tier="cheap"` when one is
    configured, else the routed client that synthesizes the answer."""
    tier = str(settings.memory_ask_deep_select_tier)
    if tier == "cheap" and llm_cheap is not None:
        return llm_cheap
    return client


async def _run_deep_pass(
    client: Any,
    search_input: Any,
    ranked: list[Any],
    memories: list[Any],
    *,
    trigger: str,
    selector: str,
    settings: Any,
) -> Any:
    """The deep-evidence selection over the ranked pool —
    shared by the serial call and the pre-started task."""
    from server.memory.app.deep_evidence import deep_evidence_pass

    pool_n = int(settings.memory_ask_deep_evidence_pool)
    return await deep_evidence_pass(
        llm=client if selector == "llm" else None,
        question=search_input.query,
        candidates=ranked[:pool_n] or memories,
        org=str(getattr(search_input, "org", "") or ""),
        namespace=str(getattr(search_input, "namespace", "") or ""),
        reason=trigger,
        max_memories=int(settings.memory_ask_deep_evidence_max_memories),
        max_chars_per_memory=int(settings.memory_ask_deep_evidence_max_chars_per_memory),
        max_chars_total=int(settings.memory_ask_deep_evidence_max_chars),
    )
