"""Whether an ask synthesizes at all: the gate override for shapes the
similarity gate mis-scores, and the early answers (abstention, record,
retrieval-only, capped) with the identifier to hedge on."""

from __future__ import annotations

import logging
from typing import Any

from server.memory.app.ask.pool import _AskRun
from server.memory.app.ask.question_shapes import (
    _HOW_MANY_Q_RE,
    _count_parts_in_pool,
    _parse_options_safe,
    is_affect_intent,
    is_arithmetic_question,
    is_mention_time_question,
)
from server.memory.app.ask.records import _gh_items_fallback_answer
from server.memory.app.ask.result import MemoryAskResult, _keys_of

logger = logging.getLogger(__name__)


def _gate_override(question: str, retrieval: Any, results: list[Any], _settings: Any) -> bool:
    """The shapes where the similarity gate's measure is the wrong one and
    synthesis runs on the rows it returned: affect at a moment, a forced
    choice, a count from parts, arithmetic over dated rows."""

    # A sentiment-at-a-moment question is answered by the
    # cues nearest the asked time, not by how much a memory resembles the
    # question — "what was my mood around 07:40" and "a sudden chill seeped
    # through the trees" share no vocabulary and the similarity gate would
    # abstain with the right dated chunks in hand. Time proximity is the
    # relevance signal here: synthesize when anything came back.
    affect_time_anchored = (
        retrieval.abstain
        and bool(results)
        and getattr(retrieval, "abstain_code", "")
        in ("below_threshold", "low_calibrated_confidence")
        and is_affect_intent(question)
    )
    # A question that lists its options is a forced choice: the similarity gate measures how
    # much a memory resembles the question, and "the time allocated for the activity that
    # lasts one day" resembles no memory while the pool holds the duration row and its
    # siblings. With results in hand, synthesize; the option programs check the letter.
    # Without this a multiple-choice item can abstain at 0.26 against a 0.27 gate with the answer's
    # rows retrieved.
    forced_choice = (
        retrieval.abstain
        and bool(results)
        and getattr(retrieval, "abstain_code", "")
        in ("below_threshold", "low_calibrated_confidence")
        and len(_parse_options_safe(question)) >= 2
    )
    # A count/set question whose pool holds the category's
    # parts is answered from them, not abstained on the rank-1 cosine.
    forced_count = (
        retrieval.abstain
        and bool(results)
        and getattr(retrieval, "abstain_code", "")
        in ("below_threshold", "low_calibrated_confidence")
        and _count_parts_in_pool(question, results)
    )
    # Arithmetic over dated rows — the gate cannot score a
    # difference of two dates by how much either row resembles the question.
    forced_arithmetic = (
        retrieval.abstain
        and bool(results)
        and getattr(retrieval, "abstain_code", "")
        in ("below_threshold", "low_calibrated_confidence")
        and is_arithmetic_question(question)
    )
    if forced_arithmetic:
        logger.info(
            "memory.ask: calibration abstain (%s) bypassed for an arithmetic question; "
            "%d row(s) go to synthesis",
            getattr(retrieval, "abstain_code", ""),
            len(results),
        )
    # A mention-time lookup ("I mentioned an investment four
    # weeks ago — what did I buy?") or a count over the pool ("how many sports
    # have I played competitively") whose rows came back is synthesized too;
    # without this both shapes can return an EMPTY answer from the gate with
    # the answering rows in hand.
    forced_lookup = (
        retrieval.abstain
        and bool(results)
        and getattr(retrieval, "abstain_code", "")
        in ("below_threshold", "low_calibrated_confidence")
        and (is_mention_time_question(question) or bool(_HOW_MANY_Q_RE.search(question or "")))
    )
    if forced_lookup and not (forced_count or forced_arithmetic):
        logger.info(
            "memory.ask: calibration abstain (%s) bypassed for a lookup/count question; "
            "%d row(s) go to synthesis",
            getattr(retrieval, "abstain_code", ""),
            len(results),
        )
    # With rows in hand, a calibration abstain never ends the
    # ask on its own — the synthesizer reads the rows and may still say the
    # memories are silent. "What speed is my new internet plan?" returned an
    # EMPTY answer with "upgraded to 500 Mbps three weeks ago" in the pool;
    # the four shape-specific exemptions above are each one more instance of
    # the same mismatch.
    forced_any = (
        retrieval.abstain
        and bool(results)
        and getattr(retrieval, "abstain_code", "")
        in ("below_threshold", "low_calibrated_confidence")
    )
    return bool(
        affect_time_anchored
        or forced_choice
        or forced_count
        or forced_arithmetic
        or forced_lookup
        or forced_any
    )


async def _early_answer(
    run: _AskRun,
    *,
    allow_synthesis: bool,
    budget_capped: bool,
    llm: Any,
    gh_items_fallback: bool | None,
) -> tuple[MemoryAskResult | None, str]:
    """Decide whether this ask synthesizes at all. Returns (the answer to
    serve now, the identifier to hedge on): an abstention, a GitHub-items
    record, a retrieval-only or capped answer comes back as the first value;
    None means synthesize, hedging on the second value when it is set."""
    search_input, question, _settings = run.search_input, run.question, run.settings
    # ---- abstention flows through: retrieval abstains -> ask abstains -------
    # Exception: identifier-grounding (abstain_code "entity_absent") is a COSINE
    # guard that cannot distinguish "genuine content, identifier just not cited
    # verbatim" from "wrong entity" — on a "what do we know about PR 12345"
    # query it abstains even when the top hit is exactly that PR's content.
    # When it fires but on-topic results exist, don't return silence: synthesize
    # and let the LLM (which reads the content) judge grounding — answer when the
    # memories genuinely concern the identifier, hedge when they only relate.
    grounding_absent = getattr(run.retrieval, "abstain_code", "") == "entity_absent" and bool(
        run.results
    )
    gate_override = _gate_override(question, run.retrieval, run.results, _settings)
    if (run.retrieval.abstain and not grounding_absent and not gate_override) or not run.results:
        # A PR/issue number loom's GitHub-items index knows is not "no
        # memory": answer from the item's own record.
        _gh = await _gh_items_fallback_answer(
            question, search_input, _settings, run.results, run.model, gh_items_fallback
        )
        if _gh is not None:
            return _gh, ""
        return MemoryAskResult(
            source="abstained",
            abstain=True,
            abstain_reason=(run.retrieval.abstain_reason or "no memories matched the query"),
            results=run.results,
            embedding_model=run.model,
        ), ""
    # Hedge only for identifiers STILL absent from the synthesis pool after
    # injection — if injection surfaced the entity, answer it normally.
    hedge_ident = ""
    if grounding_absent:
        _covered = " ".join((getattr(h, "content_excerpt", "") or "") for h in run.synth_pool)
        _still_missing = [i for i in run.idents if i not in _covered]
        hedge_ident = _still_missing[0] if _still_missing else ""
        # The other live shape of the same loss: retrieval
        # returned neighbours ABOUT OTHER PRs and the identifier is not in the
        # pool, so synthesis would hedge ("no memory specifically about #N")
        # after ten seconds of model time. If the GitHub-items index knows
        # the number, its record is the answer.
        if hedge_ident:
            _gh = await _gh_items_fallback_answer(
                question, search_input, _settings, run.results, run.model, gh_items_fallback
            )
            if _gh is not None:
                return _gh, hedge_ident

    # ---- retrieval-only (agent synthesizes) — parity with code.ask ----------
    if not allow_synthesis:
        return MemoryAskResult(
            source="retrieval_only",
            answer="",
            memory_keys=_keys_of(run.results),
            results=run.results,
            embedding_model=run.model,
            facets=run.facets,
            facet_memories_added=run.facet_added,
            pool_k=run.pool_k_used,
        ), hedge_ident

    # ---- cost-cap gate: be EXPLICIT. The code plane has a known bug where a
    # capped call passes llm=None and returns source="synthesized" with an
    # empty answer — indistinguishable from a failed synthesis to the caller.
    # Here a capped call is an honest retrieval_only carrying "budget_capped".
    if budget_capped:
        return MemoryAskResult(
            source="retrieval_only",
            answer="",
            abstain_reason="budget_capped",
            memory_keys=_keys_of(run.results),
            results=run.results,
            embedding_model=run.model,
            pool_k=run.pool_k_used,
        ), hedge_ident

    # Defensive: no client available (e.g. keyless dev) — degrade to
    # retrieval_only rather than pretend to synthesize.
    if llm is None:
        return MemoryAskResult(
            source="retrieval_only",
            answer="",
            memory_keys=_keys_of(run.results),
            results=run.results,
            embedding_model=run.model,
            facets=run.facets,
            facet_memories_added=run.facet_added,
            pool_k=run.pool_k_used,
        ), hedge_ident
    return None, hedge_ident
