"""The extraction entry point: `extract_from_messages` (the general path,
with its retries, sweep, backfill, deterministic passes and judge).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from server.llm.cache import scoped_key
from server.llm.client import LLMResult, get_llm_client
from server.memory.app.extract.client import (
    _client_cache_counts,
    _extractor_client,
    _merge_llm_results,
)
from server.memory.app.extract.coverage import statement_dense_turns, uncovered_statement_count
from server.memory.app.extract.dates import (
    _apply_relative_date_grounding,
    _apply_temporal_anchor_backstop,
    apply_message_timestamps,
)
from server.memory.app.extract.enrich import _apply_code_facets, _recover_rows, _repair
from server.memory.app.extract.judge import _judge_facts
from server.memory.app.extract.keys import (
    _sanitize_key,
    apply_relation_keys,
    normalize_attribute_keys,
    rewrite_fact_key,
    scope_session_state_keys,
    strip_value_suffixes,
    strip_variant_suffixes,
)
from server.memory.app.extract.models import (
    EXTRACTION_PROMPT_VERSION,
    ExtractedFact,
    ExtractionError,
    ExtractionResult,
)
from server.memory.app.extract.parse import (
    _TRUNCATION_RETRY_MAX_TOKENS,
    _looks_truncated,
    _parse_fact_list,
    _salvage_fact_objects,
)
from server.memory.app.extract.prompts import (
    _build_extraction_user_prompt,
    _extraction_base_prompt,
    _use_compact_prompt,
)
from server.memory.app.extract.relations import _backfill_relations
from server.memory.app.extract.speakers import (
    apply_message_speakers,
    attribute_assistant_introduced,
)
from server.memory.app.extract.sweep import _coverage_sweep
from server.memory.domain.extraction_policy import with_policy
from server.memory.domain.extraction_prompts import (
    _NO_RECITATION_ADDENDUM,
    _RETRY_PROMPT_ADDENDUM,
    with_strict_examples_preamble,
)
from server.memory.domain.grounding import grade_facts
from server.observability.genai import staged
from server.settings import settings

log = logging.getLogger("server.memory.app.extract")


# Threshold for "non-trivial input" that *should* yield at least one
# fact. Below this, zero-fact results are accepted as legitimate (the
# user said "hi", there's nothing memorable). Above it, zero facts is
# a signal the LLM misfired and we retry once with a stricter prompt.
# 40 chars + sentence punctuation catches real one-liners like "My dog
# Bowie is seven." while still skipping "hi" / "ok thanks".
_NONTRIVIAL_INPUT_CHARS = 40


def _is_nontrivial(messages: list[dict[str, object]]) -> bool:
    """Heuristic: is the input substantive enough that zero facts looks
    like an extraction misfire vs a legitimately empty conversation?

    At least `_NONTRIVIAL_INPUT_CHARS` content characters across the
    messages and at least one sentence-ending mark. Greetings ("hi",
    "ok thanks") fall below threshold; multi-sentence messages above.
    Stops at the first message that settles both (it runs up to three
    times per extraction, over the whole transcript)."""
    total_chars = 0
    has_sentence = False
    for m in messages:
        c = str(m.get("content", "")).strip()
        total_chars += len(c)
        has_sentence = has_sentence or "." in c or "!" in c or "?" in c
        if has_sentence and total_chars >= _NONTRIVIAL_INPUT_CHARS:
            return True
    return False


async def _zero_fact_retry(
    client: Any,
    *,
    system: str,
    user: str,
    max_tokens: int,
    response_format: dict[str, str],
    parse: Callable[[str], list[ExtractedFact]],
    discards: list[str],
    accumulated: LLMResult,
) -> tuple[list[ExtractedFact], LLMResult]:
    """The one retry of a first pass that parsed to zero facts on non-trivial input:
    (facts, usage so far). The retry's tokens were spent whatever it found; its reply
    replaces the first only when it carries facts (the first pass's discards stand
    otherwise). The first pass completed, so a retry reply that does not parse is a
    retry that found nothing, not a failed extraction."""
    retry_result = await client.complete(
        system=system, user=user, max_tokens=max_tokens, response_format=response_format
    )
    first_discards = list(discards)
    try:
        retry_facts = parse(retry_result.text)
    except ExtractionError as e:
        log.warning("zero-fact retry reply unparseable; the first pass stands: %s", e)
        retry_facts = []
    if retry_facts:
        return retry_facts, _merge_llm_results(accumulated, retry_result)
    discards[:] = first_discards
    return [], _merge_llm_results(accumulated, retry_result, text=accumulated.text)


async def _judge_gate(
    messages: list[dict[str, object]],
    facts: list[ExtractedFact],
    accumulated: LLMResult,
    *,
    observation_date: str | None,
    client: Any,
) -> tuple[list[ExtractedFact], LLMResult, int]:
    """(facts the judge kept, the usage with the judge call counted, WRONG drops).
    The extraction's own reply text stays the accumulated text."""
    verdicts, judge_result = await _judge_facts(
        messages, facts, observation_date=observation_date, client=client
    )
    if judge_result is not None:
        accumulated = _merge_llm_results(accumulated, judge_result, text=accumulated.text)
    if not verdicts:
        return facts, accumulated, 0
    kept: list[ExtractedFact] = []
    dropped = 0
    for f in facts:
        v = verdicts.get(f.memory_key, "")
        if v == "WRONG":
            # Drop fabricated / unsupported facts — but a fully extracted fact
            # leaving here must be visible: count it and name the key.
            dropped += 1
            log.info("extraction judge dropped fact %r (verdict=WRONG)", f.memory_key)
            continue
        kept.append(replace(f, judge_verdict=v))
    return kept, accumulated, dropped


def _extraction_system_prompt(
    compact: bool,
    *,
    existing_memories: list[dict[str, str]] | None,
    namespace_categories: list[dict[str, str]] | None,
    custom_instructions: str,
) -> str:
    """The extractor's system prompt: the base prompt under the strict-examples
    preamble, the fence note, the namespace's categories and the caller's
    instructions. Each call appends the operator's policy after it
    (`with_policy`)."""
    system_prompt = with_strict_examples_preamble(_extraction_base_prompt(compact))
    if existing_memories:
        from server.memory.domain.prompt_fence import FENCE_NOTE

        # The one sentence that says what the markers around
        # `existing_memories` mean. Only when something is fenced.
        system_prompt = system_prompt + "\n\n" + FENCE_NOTE
    # Surface namespace-registered categories so emitted tags align with
    # the operator's taxonomy instead of inventing synonyms.
    if namespace_categories:
        cat_lines = "\n".join(
            f"  - {c['name']}: {(c.get('description') or '').strip()[:160]}"
            for c in namespace_categories[:32]
        )
        system_prompt = (
            system_prompt + "\n\n=============================================="
            "\nNAMESPACE CATEGORIES (PREFER THESE TAGS)"
            "\n=============================================="
            "\nThis namespace has the following registered categories. "
            "When a fact fits one of these, include the category NAME "
            "in the `tags` array. Do not invent synonyms when an "
            "existing category covers the fact.\n"
            f"\n{cat_lines}\n"
        )
    if custom_instructions.strip():
        system_prompt = (
            system_prompt + "\n\n=============================================="
            "\nADDITIONAL CALLER INSTRUCTIONS"
            "\n=============================================="
            f"\n{custom_instructions.strip()}\n"
        )
    return system_prompt


def _parses(parse: Any, text: str) -> bool:
    try:
        parse(text)
    except ExtractionError:
        return False
    return True


def _accumulate(calls: list[Any]) -> Any:
    """Every call's spend in one result; the text is the last call's."""
    total = calls[0]
    for res in calls[1:]:
        total = _merge_llm_results(total, res)
    return total


# Stop reasons that mean the provider cut the reply, so its text is partial.
_FILTERED_STOPS = frozenset({"content_filter", "refusal"})


async def _complete_and_parse(
    client: Any,
    *,
    base_prompt: str,
    user_prompt: str,
    max_tokens: int,
    json_format: dict[str, str],
    parse: Any,
    operator_policy: str,
) -> tuple[Any, list[ExtractedFact], str]:
    """The extraction call and its parse, with one retry on an unparseable
    answer: a larger budget when it reads as truncated (salvaging complete
    fact objects if it truncates again), else the stricter addendum. Returns
    (accumulated LLM result, facts, truncation). `base_prompt` is the system
    prompt without the operator's policy: every call, retries included, appends
    the policy to it."""
    calls: list[Any] = []

    async def complete(base: str, tokens: int) -> Any:
        """One completion; a reply the provider's filter cut (a quoted passage)
        is asked again without the recitation, since a bigger budget is cut at
        the same place."""
        system = with_policy(base, operator_policy)
        res = await client.complete(
            system=system, user=user_prompt, max_tokens=tokens, response_format=json_format
        )
        calls.append(res)
        if getattr(res, "from_cache", False) and not _parses(parse, res.text):
            # A cached reply that does not parse (cut before the cache knew to
            # refuse it) is not trusted: the provider answers again.
            from server.llm.cache import bypass

            with bypass():
                res = await client.complete(
                    system=system, user=user_prompt, max_tokens=tokens, response_format=json_format
                )
            calls.append(res)
        if str(getattr(res, "stop_reason", "") or "") in _FILTERED_STOPS:
            res = await client.complete(
                system=with_policy(base + _NO_RECITATION_ADDENDUM, operator_policy),
                user=user_prompt,
                max_tokens=tokens,
                response_format=json_format,
            )
            calls.append(res)
        return res

    result = await complete(base_prompt, max_tokens)
    _parse = parse
    truncation = ""
    try:
        facts = _parse(result.text)
    except ExtractionError:
        if _looks_truncated(result.text):
            # Truncation-shaped failure: the same budget truncates at the
            # same place, so the retry RAISES max_tokens instead of
            # re-rolling. If the enlarged attempt still truncates, salvage
            # the complete fact objects from the longer payload rather than
            # failing the whole ingest.
            retry_tokens = max(max_tokens, min(max_tokens * 2, _TRUNCATION_RETRY_MAX_TOKENS))
            retry_result = await complete(base_prompt, retry_tokens)
            try:
                facts = _parse(retry_result.text)
                truncation = "retried"
            except ExtractionError:
                salvaged = _salvage_fact_objects(retry_result.text) or _salvage_fact_objects(
                    result.text
                )
                if salvaged is None:
                    raise
                facts = _parse(json.dumps({"facts": salvaged}))
                truncation = "salvaged"
        else:
            retry_result = await complete(base_prompt + _RETRY_PROMPT_ADDENDUM, max_tokens)
            facts = _parse(retry_result.text)
    return _accumulate(calls), facts, truncation


def _coverage_sweep_reason(
    coverage_sweep: bool | None,
    coverage_sweep_source: str,
    messages: list[dict[str, object]],
    facts: list[ExtractedFact],
) -> tuple[str, bool]:
    """Why the coverage sweep runs ('' when nothing asks for it), and whether
    the first pass's uncovered statements are what fired it."""
    if coverage_sweep is not None:
        coverage_sweep_reason = coverage_sweep_source or "override"
    else:
        _n_dense = statement_dense_turns(
            messages,
            min_statements=int(settings.memory_extract_coverage_sweep_auto_min_statements),
        )
        coverage_sweep_reason = f"list:{_n_dense}" if _n_dense else ""
    # A conversation ingest gets the second reading
    # when the first pass visibly left statements behind — measured on
    # multi-session conversations with the slim prompt: the sweep's rows were exactly the missing
    # set items (figurines, peach cobbler, taekwondo), multi-hop 54 → 63.
    _uncovered_fires = False
    if coverage_sweep is None and not coverage_sweep_reason:
        _min_unc = int(settings.memory_extract_coverage_sweep_auto_min_uncovered)
        if _min_unc > 0 and _is_nontrivial(messages):
            _n_unc = uncovered_statement_count(messages, facts)
            if _n_unc >= _min_unc:
                _uncovered_fires = True
                coverage_sweep_reason = f"uncovered:{_n_unc}"
    return coverage_sweep_reason, _uncovered_fires


def _resolve_index_links(
    facts: list[ExtractedFact], existing_memories: list[dict[str, str]]
) -> list[ExtractedFact]:
    """Resolve integer-index linked_keys ("__idx:N") to real memory_keys via
    existing_memories (the LLM links by index, not by key)."""
    resolved: list[ExtractedFact] = []
    for f in facts:
        new_lk: list[str] = []
        for lk in f.linked_keys:
            if lk.startswith("__idx:"):
                try:
                    idx = int(lk[len("__idx:") :])
                except ValueError:
                    continue
                if 0 <= idx < len(existing_memories):
                    mk = existing_memories[idx].get("memory_key", "")
                    if mk:
                        new_lk.append(_sanitize_key(mk))
            else:
                new_lk.append(lk)
        resolved.append(replace(f, linked_keys=new_lk))
    return resolved


def _settle_facts(
    facts: list[ExtractedFact],
    messages: list[dict[str, object]],
    *,
    observation_date: str | None,
    existing_memories: list[dict[str, str]] | None,
    speaker_attribution: bool,
    recover_rows: bool,
) -> list[ExtractedFact]:
    """The deterministic passes between the model's answer and the judge:
    stable and relation keys, citation repair, temporal anchors and
    timestamps, suffix and attribute normalisation, relative-date grounding,
    speakers, code facets, index links, the grounding grade and speaker
    attribution. Pure."""
    # Entity-stable key rewrite: turn a concept-level key
    # (`user_task_status`) into an entity-prefixed one (`pr107585_status`)
    # when the fact carries a salient identifier. Applied here so every
    # caller (pipeline `_step_llm_extract` reads `f.memory_key`, SDK, the
    # judge, dedup) sees the stable key.
    if facts:
        # Per-run state facts first take the session's one task slug as their
        # entity, then every fact gets the per-fact rewrite.
        facts = scope_session_state_keys(facts)
        facts = [rewrite_fact_key(f) for f in facts]

    # The key of a fact that carries one single-valued relation
    # is the relation, not the model's wording.
    if facts:
        facts = apply_relation_keys(facts)

    # Citation repair runs before anything reads `source_message_indices`
    # (the backstop, timestamps, speakers, grounding grade, code facets),
    # with or without an observation date.
    facts = _repair(facts, messages)
    # Deterministic temporal-anchor backstop: prompt-level resolution is
    # the primary path; this catches facts where the model kept a
    # closed-class relative qualifier verbatim and emitted no anchor.
    if facts and observation_date:
        facts = _apply_temporal_anchor_backstop(facts, observation_date, messages)
    # The rows the model left out are recovered from the turns, unless an
    # operator policy is in force: the model may have left them out for it.
    if recover_rows:
        facts = _recover_rows(facts, messages)
    facts = apply_message_timestamps(facts, messages)
    facts = strip_variant_suffixes(facts)
    if facts:
        facts = strip_value_suffixes(facts)
    facts = normalize_attribute_keys(facts)
    # The anchor is now final; make the TEXT say it when it still carries the
    # relative phrase. After the timestamp pass so a turn-time
    # anchor is what gets written.
    if facts:
        facts = _apply_relative_date_grounding(facts, observation_date or "")
    facts = apply_message_speakers(facts, messages)

    # Deterministic code-native facets: populates files[] + concepts[]
    # without touching the atomic fact value.
    if facts:
        facts = _apply_code_facets(facts, messages)

    if facts and existing_memories:
        facts = _resolve_index_links(facts, existing_memories)

    # Grounding grade: tie each fact to its turns by a
    # verified span, a citation, or nothing — before the judge, so the judge
    # sees the grade too. Pure; never drops a fact.
    facts = grade_facts(facts, messages)

    # Speaker attribution: an item the ASSISTANT introduced
    # that the extractor wrote as the user's interest is re-attributed from
    # the evidence itself — the item's name occurs in assistant turns and in
    # no user turn — so "what have you recommended to me?" finds it. Pure.
    facts = attribute_assistant_introduced(facts, messages) if speaker_attribution else facts
    return facts


@staged("extract")
async def extract_from_messages(
    messages: list[dict[str, object]],
    *,
    max_tokens: int = 1024,
    observation_date: str | None = None,
    existing_memories: list[dict[str, str]] | None = None,
    last_messages: list[dict[str, object]] | None = None,
    custom_instructions: str = "",
    operator_policy: str,
    judge: bool = True,
    namespace_categories: list[dict[str, str]] | None = None,
    coverage_sweep: bool | None = None,
    coverage_sweep_source: str = "",
    speaker_attribution: bool = True,
    org: str = "",
    namespace: str = "",
    llm_cache: bool | None = None,
    compact_prompt: bool | None = None,  # None = judge `messages`
) -> ExtractionResult:
    """Run one LLM completion to extract memorable facts from messages.

    `observation_date` (ISO YYYY-MM-DD) is the "now" anchor the LLM uses
    to resolve relative time references like "last week" → absolute
    dates. When omitted, the LLM keeps relative references verbatim.

    `existing_memories` is a list of `{memory_key, value}` dicts the
    extractor uses for write-time deduplication and refinement linking.
    Pass the last ~20 facts under the same identity so the LLM doesn't
    re-extract things you already know.

    `operator_policy` is the operator's extraction policy for the namespace
    (`extraction_policy.effective`): it ends the system prompt of every call,
    retries and the coverage sweep included, above `custom_instructions`. While
    it is set, no row is recovered from the turns past the model.

    `judge` enables the LLM-judge gate. After extraction, a second
    LLM call grades each fact CORRECT/UNCERTAIN/WRONG against the
    source messages. WRONG facts are dropped before return; UNCERTAIN
    survives with the verdict attached. Adds one LLM call per write.

    Raises `ExtractionError` if the LLM response isn't parseable. Callers
    should let that bubble up as a 502 — silently writing zero memories
    is worse than telling the caller something's wrong with their
    extractor.

    On non-trivial input that yields zero facts (a likely extraction
    misfire), one retry with a stricter prompt addendum is attempted.
    Token cost in the misfire case: one extra LLM call. In the
    legitimately-empty case (greetings, trivial replies): no retry.
    """
    if not messages:
        client = get_llm_client()
        # Avoid an LLM call on empty input.
        return ExtractionResult(
            facts=[],
            llm_result=LLMResult(
                text="", tokens_in=0, tokens_out=0, provider=client.provider, model=client.model
            ),
            prompt_version=EXTRACTION_PROMPT_VERSION,
        )
    client = _extractor_client(org=org, namespace=namespace, llm_cache=llm_cache)
    user_prompt = _build_extraction_user_prompt(
        messages,
        observation_date,
        existing_memories,
        last_messages,
    )
    compact = _use_compact_prompt(messages) if compact_prompt is None else compact_prompt
    # The prompt without the operator's policy; each call appends the policy
    # last, after any retry addendum, so nothing follows it.
    base_prompt = _extraction_system_prompt(
        compact,
        existing_memories=existing_memories,
        namespace_categories=namespace_categories,
        custom_instructions=custom_instructions,
    )
    system_prompt = with_policy(base_prompt, operator_policy)
    # JSON mode constrains the model to valid JSON; the try/except below
    # is a single belt-and-suspenders parse-retry on top of it.
    json_format = {"type": "json_object"}
    prompt_key = scoped_key(system_prompt, user_prompt, max_tokens, json_format, org=org)
    parse_discards: list[str] = []

    def _parse(text: str) -> list[ExtractedFact]:
        # Discards belong to the parse that produced `facts`; reset per call.
        parse_discards.clear()
        return _parse_fact_list(text, discards=parse_discards)

    accumulated, facts, truncation = await _complete_and_parse(
        client,
        base_prompt=base_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        json_format=json_format,
        parse=_parse,
        operator_policy=operator_policy,
    )

    # Validation gate: retry once on non-trivial-input + zero-facts.
    if not facts and _is_nontrivial(messages):
        facts, accumulated = await _zero_fact_retry(
            client,
            system=with_policy(base_prompt + _RETRY_PROMPT_ADDENDUM, operator_policy),
            user=user_prompt,
            max_tokens=max_tokens,
            response_format=json_format,
            parse=_parse,
            discards=parse_discards,
            accumulated=accumulated,
        )

    # Coverage sweep: the turns no fact cites get one more
    # reading, on their own. Before the judge so the sweep's facts are graded
    # like the first pass's.
    # The namespace's switch first, the ingest's shape when it has none.
    from server.memory.app.coverage_sweep_gate import effective as _sweep_effective

    coverage_swept = 0
    # Why the sweep ran, on the generation and the
    # result — "per_call" / "namespace" (the caller's or the namespace's
    # switch), "list:<n>" (the density
    # gate, n dense turns) — so the fire rate can be read per rule.
    coverage_sweep_reason, _uncovered_fires = _coverage_sweep_reason(
        coverage_sweep, coverage_sweep_source, messages, facts
    )
    if (
        _sweep_effective(coverage_sweep, settings, messages) or _uncovered_fires
    ) and _is_nontrivial(messages):
        # The sweep is a model call too: its own slim prompt ends with the policy.
        facts, _cs_result, coverage_swept = await _coverage_sweep(
            messages,
            facts,
            base_prompt=base_prompt,
            operator_policy=operator_policy,
            observation_date=observation_date,
            last_messages=last_messages,
            max_tokens=max_tokens,
            min_chars=int(settings.memory_extract_coverage_sweep_min_chars),
            max_turns=int(settings.memory_extract_coverage_sweep_max_turns),
            gate_reason=coverage_sweep_reason,
            client=client,
        )
        if _cs_result is not None:
            accumulated = _merge_llm_results(accumulated, _cs_result)

    # Relation backfill: the facts the first pass left without a typed
    # relation get one more, smaller call. Before the relation key rewrite
    # below, which keys a fact by its single-valued relation, and before the
    # judge, which then grades the backfilled facts too.
    relations_backfilled = 0
    if facts:
        facts, _bf_result, relations_backfilled = await _backfill_relations(facts, client=client)
        if _bf_result is not None:
            accumulated = _merge_llm_results(accumulated, _bf_result)
        if relations_backfilled:
            log.info("relation backfill gave %d fact(s) a typed relation", relations_backfilled)

    facts = _settle_facts(
        facts,
        messages,
        observation_date=observation_date,
        existing_memories=existing_memories,
        speaker_attribution=speaker_attribution,
        recover_rows=not operator_policy,
    )

    # Judge gate: drop WRONG, keep CORRECT/UNCERTAIN. observation_date
    # lets the judge verify temporal resolutions instead of flagging them
    # as added detail.
    dropped_judge_wrong = 0
    if judge and facts:
        facts, accumulated, dropped_judge_wrong = await _judge_gate(
            messages, facts, accumulated, observation_date=observation_date, client=client
        )

    if parse_discards:
        log.info(
            "extraction parser discarded %d fact object(s): %s",
            len(parse_discards),
            ", ".join(parse_discards[:8]),
        )
    return ExtractionResult(
        facts=facts,
        llm_result=accumulated,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        dropped_judge_wrong=dropped_judge_wrong,
        dropped_parse=len(parse_discards),
        truncation=truncation,
        relations_backfilled=relations_backfilled,
        coverage_swept=coverage_swept,
        prompt_key=prompt_key,
        **_client_cache_counts(client),
    )
