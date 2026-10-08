"""One grounded synthesis call and what follows it: the output budget, the
prompt notes, the LLM call, the deterministic answer verifiers, and the
not-recorded second reading over raw turns."""

from __future__ import annotations

import logging
import re
from typing import Any

from server.memory.app.ask.prompts import (
    _ABSTAIN_CONSERVATIVE,
    _AFFECT_SYSTEM,
    _CHAIN_NOTE,
    _COUNT_SYSTEM,
    _COUNT_SYSTEM_RULES,
    _INFERENCE_NOTE,
    _KNOWLEDGE_NOTE,
    _OPTIONS_SYSTEM,
    _PR_RECORD_NOTE,
    _PREFERENCE_SYSTEM,
    _PRESUPPOSITION_NOTE,
    _RECENT_TURNS_NOTE,
    _RECOMMENDATION_SYSTEM,
    _RECORD_NOTE,
    _SUPERSESSION_NOTE,
    _SYNTH_SYSTEM,
    _TEMPORAL_DERIVE_NOTE,
    _WORLD_KNOWLEDGE_NOTE,
    _has_pr_record,
    _window_notes,
)
from server.memory.app.ask.question_shapes import (
    _option_lines,
    is_affect_intent,
    is_count_intent,
    is_latest_word_question,
    is_preference_intent,
    is_recommendation_question,
    is_total_number_question,
)
from server.memory.app.ask.rendering import (
    _SCRATCHPAD_HEAD,
    _SOURCE_TURNS_HEAD,
    _TIMELINE_HEAD,
    _assemble_prompt,
    _context_block_chars,
    _render_window,
    elision_note,
)
from server.memory.app.ask.result import _keys_of
from server.memory.app.ask.state import (
    _LAST_COUNT_RECOMPUTED,
    _LAST_SYNTH_MODEL,
    _PERSONALIZE_OVERRIDE,
    _RECOUNT_OVERRIDE,
    _SYNTH_CALLS,
)
from server.memory.domain import ask_attribution as _attr

logger = logging.getLogger(__name__)


_ANSWER_MARK_RE = re.compile(r"(?im)^\s*ANSWER\s*:\s*")
_ITEMS_MARK_RE = re.compile(r"(?im)^\s*(ITEMS|TALLY|CUES)\s*:\s*\n?")
_CITATION_RE = re.compile(r"\[[A-Za-z0-9_:.\-]+\]")
# A worksheet enumeration line: a bullet or numbered item, or a cue / tally
# mapping ("belittled → Sadness", "Die Hard — action 1").
_ENUM_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|.*(?:→|->)|.*\s\d+\s*$)")


def fold_count_answer(text: str) -> str:
    """Reorder an enumerate-then-count reply so the ANSWER line leads, followed
    by the citations of its ITEMS enumeration (the quoted lines themselves are
    the worksheet, not the answer). A char cap applied after
    this keeps the count; without the fold it kept the first few items and
    dropped the count (aggregative multiple-choice questions: an answer that listed three
    events and ended "= 4" lost its letter to the cap). Text without an
    ANSWER marker is returned unchanged."""
    marks = list(_ANSWER_MARK_RE.finditer(text or ""))
    if not marks:
        return (text or "").strip()
    m = marks[-1]
    answer = text[m.end() :].strip()
    items = _ITEMS_MARK_RE.sub("", text[: m.start()]).strip()
    if not answer:
        return (text or "").strip()
    # The worksheet's enumeration (item bullets, cue and tally lines) is how the
    # answer was reached, not part of it: its lines go and their citations stay.
    # A prose line beside it ("The memories do not record Joanna hosting a
    # party") is part of the answer and stays.
    prose = [ln.strip() for ln in items.splitlines() if ln.strip() and not _ENUM_LINE_RE.match(ln)]
    cited = [c for c in dict.fromkeys(_CITATION_RE.findall(items)) if c not in answer]
    out = " ".join([answer, *prose]).strip()
    return f"{out} {' '.join(c for c in cited if c not in out)}".strip()


def synth_token_budget(
    *,
    window: int,
    evidence_tokens: int,
    ratio: float,
    fixed: int = 0,
    ceiling: int = 8000,
) -> int:
    """Output-token budget for one synthesis call.

    `fixed` > 0 wins (an operator insisted). Otherwise the window rule —
    600 at a window of 15 or fewer memories, 900 above — is the FLOOR, and
    the budget grows with the evidence: `ratio * evidence_tokens`, capped at
    `ceiling`. A 25-row window of ~4k evidence tokens at ratio 0.5
    gets ~2k tokens; a three-fact persona window keeps 600. The model stops
    on its own when the evidence is thin, so the budget is room, not a
    target."""
    floor = 600 if int(window) <= 15 else 900
    if fixed and int(fixed) > 0:
        return int(fixed)
    ceiling = max(floor, int(ceiling) if ceiling else floor)
    adaptive = int(float(ratio) * max(0, int(evidence_tokens))) if ratio and ratio > 0 else 0
    return max(floor, min(ceiling, adaptive))


def _context_notes(
    excerpts: str,
    question: str,
    memories: list[Any],
    *,
    order_mode: str,
    intent: Any,
    omitted: list[Any] | None,
    graph_note: str,
    recent_block: str,
    presupposition: bool,
    record_note: bool,
    inference_note: bool,
    temporal_derive: bool,
    chain_note: bool,
    scratchpad: str,
    world_knowledge: bool,
    timeline: str,
    raw_evidence: str,
    knowledge_block: str,
    as_of: Any,
    premise_system: str,
    hedge_ident: str,
) -> tuple[str, list[str]]:
    """The excerpts with the context blocks around them, and the per-call
    notes the system prompt gets, in the order they are appended."""
    note = elision_note(omitted or [])
    if note:
        excerpts = f"{excerpts}\n\n{note}"
    if graph_note:
        excerpts = f"{excerpts}\n\n{graph_note}"
    if recent_block:
        # The turns the question sits in come first: they are what "it",
        # "she" and "that" refer to. Facts still come from the memories.
        excerpts = f"{recent_block}\n\n{excerpts}"
    # The per-call notes, appended to the system prompt in this order.
    _notes: list[str] = []
    if recent_block:
        _notes.append(f" {_RECENT_TURNS_NOTE}")
    if presupposition:
        _notes.append(f" {_PRESUPPOSITION_NOTE}")
    if record_note:
        _notes.append(f" {_RECORD_NOTE}")
    if inference_note:
        _notes.append(f" {_INFERENCE_NOTE}")
    if temporal_derive:
        _notes.append(f" {_TEMPORAL_DERIVE_NOTE}")
    _notes.extend(_window_notes(question, memories))
    # The code plane's PR record outranks a memory's state claim.
    if _has_pr_record(memories):
        _notes.append(f" {_PR_RECORD_NOTE}")
    # The later-dated position is the current one.
    from server.settings import settings as _sup_settings

    if int(_sup_settings.memory_ask_identifier_inject_recent) > 0 and is_latest_word_question(
        question
    ):
        _notes.append(f" {_SUPERSESSION_NOTE}")
    if chain_note:
        _notes.append(f" {_CHAIN_NOTE}")
    if scratchpad:
        # The agent's own task state comes first; it frames
        # the question, the memories supply the facts to cite.
        excerpts = f"{_SCRATCHPAD_HEAD}{scratchpad}\n\n{excerpts}"
        _notes.append(
            " The agent's run notes precede the memories: they are the current "
            "state of the task as the agent recorded it. Use them to interpret the "
            "question and to avoid re-deriving what the agent already concluded; cite "
            "memories, not the notes, for facts."
        )
    if world_knowledge:
        _notes.append(f" {_WORLD_KNOWLEDGE_NOTE}")
    if timeline:
        # The arithmetic is done; the model reads it.
        excerpts = f"{_TIMELINE_HEAD}{timeline}\n\n{excerpts}"
        _notes.append(
            " A timeline with computed day/week/month distances precedes the "
            "memories; answer date questions from those distances and state the "
            "dates you used."
        )
    if raw_evidence:
        # Deep-evidence pass: the source turns behind the
        # selected memories follow the excerpts. They carry the same
        # [memory_key] labels so citations keep pointing at memories.
        excerpts = f"{excerpts}\n\n{_SOURCE_TURNS_HEAD}{raw_evidence}"
        _notes.append(
            " Source turns are appended after the memories; when a "
            "memory is a summary of a turn, PREFER THE TURN'S EXACT WORDING, "
            "numbers and dates, and cite the memory key the turn is labelled "
            "with."
        )
    if knowledge_block:
        # Passages from the knowledge plane follow the memories.
        excerpts = f"{excerpts}\n\n{knowledge_block}"
        _notes.append(f" {_KNOWLEDGE_NOTE}")
    if order_mode == "chronological":
        _notes.append(
            " The memories are listed in CHRONOLOGICAL order (oldest "
            "first); use that order to resolve sequence, duration and 'what "
            "changed' questions, and state dates explicitly."
        )
    elif order_mode == "reverse_chronological":
        _notes.append(
            " The memories are listed NEWEST FIRST; the first entries are the current state."
        )
    # A relative time expression the grammar resolved ("two months ago" ->
    # 2023-01-10) is told to the reader as an absolute date: the memories are
    # dated absolutely, and the loss analysis found the reader picking a
    # same-topic memory from four months earlier when left to guess.
    _rdates = tuple(getattr(intent, "resolved_dates", ()) or ()) if intent is not None else ()
    if _rdates:
        _notes.append(
            " The question's relative time expression resolves to about "
            f"{', '.join(str(d)[:10] for d in _rdates[:3])}; when several memories "
            "match the topic, prefer the one whose date is closest to that."
        )
    if as_of is not None:
        anchor = str(as_of)[:19]
        _notes.append(
            f" Treat {anchor} as the present moment. Report the state as "
            f"of then and do NOT mention or assume any PR, issue, status change, "
            f"or event dated after {anchor}, even if a memory below records one "
            "— answer only from what was known by the anchor."
        )
    if premise_system:
        _notes.append(premise_system)
    if hedge_ident:
        _notes.append(
            f" The question references '{hedge_ident}', which is not "
            "cited verbatim in these memories. If the memories genuinely concern "
            "it, answer with their specifics. If they concern a DIFFERENT "
            "PR/issue/test/entity, do NOT present those details as if they "
            f"answer about '{hedge_ident}' — state plainly that you have no "
            f"memory specifically about '{hedge_ident}', then, only if useful, "
            "mention the closest related item clearly labeled as related (not "
            "as the answer). Never attribute another entity's facts to "
            f"'{hedge_ident}'."
        )
    return excerpts, _notes


def _answer_mode_notes(question: str, intent: Any, _notes: list[str]) -> tuple[bool, bool]:
    """The structured answering mode notes (count, preference, affect,
    recommendation); returns (count_mode, structured_mode)."""
    # Enumerate-then-count for count-shaped questions. The
    # reader's failure mode on aggregative multiple-choice questions was counting, not recall —
    # evidence recall 1.0, answer off by one — so the model lists first and
    # counts the lines; the ANSWER line is folded to the front below so the
    # char cap keeps the count, not the list.
    # Answer options that are LISTS: compare item by item.
    # A multi-session "what have you recommended" question offers four lists
    # differing by one title; the reader named all three items and picked
    # the option missing one.
    if _option_lines(question) >= 2:
        _notes.append(_OPTIONS_SYSTEM)
    count_mode = is_count_intent(intent) or is_total_number_question(question)
    # Preference questions: enumerate the items, tally the
    # attribute, answer the majority — the same list-then-read discipline.
    pref_mode = (not count_mode) and is_preference_intent(intent, question)
    # Emotion at a moment: quote the user's own cues near the
    # asked time, map each to a basic emotion, answer the strongest — the
    # reader was answering "negative/distressed" and picking a letter at random.
    affect_mode = (not count_mode) and (not pref_mode) and is_affect_intent(question)

    if count_mode:
        _notes.append(f"{_COUNT_SYSTEM}{_COUNT_SYSTEM_RULES}")
    elif pref_mode:
        _notes.append(_PREFERENCE_SYSTEM)
    elif affect_mode:
        _notes.append(_AFFECT_SYSTEM)
    # Recommendation grounding: independent of the structured
    # modes — a recommendation is not a count, a tally or a mood.
    _pz_override = _PERSONALIZE_OVERRIDE.get()
    _pz_on = True if _pz_override is None else bool(_pz_override)
    if _pz_on and is_recommendation_question(question):
        _notes.append(_RECOMMENDATION_SYSTEM)
    structured_mode = count_mode or pref_mode or affect_mode
    return count_mode, structured_mode


def _answer_budget(
    memories: list[Any],
    max_answer_chars: int,
    *,
    count_mode: bool,
    structured_mode: bool,
    notes: list[str],
) -> tuple[int, int]:
    """(max_tokens, hard_cap) for the synthesis call, with the answer-length
    note the caller's char cap asks for."""
    from server.settings import settings as _s

    _notes = notes
    # Answer-budget: scale max_tokens to the char cap (~4 chars/token) and ask
    # for a concise answer, then hard-truncate as the backstop. 0 = unbounded.
    # Room to enumerate a full set (breadth queries can need 15-25 items, each
    # with its own specifics) without truncation; single-fact answers stay short.
    _win = int(_s.memory_ask_context_memories)
    # The budget follows the evidence in the window.
    _evidence_tokens = sum(len(str(getattr(m, "content_excerpt", "") or "")) for m in memories) // 4
    max_tokens = synth_token_budget(
        window=_win,
        evidence_tokens=_evidence_tokens,
        ratio=float(_s.memory_ask_synth_budget_ratio),
        fixed=int(_s.memory_ask_synth_max_tokens),
        ceiling=int(_s.memory_ask_synth_max_tokens_ceiling),
    )
    # The char backstop scales with the budget (~4 chars/token); 6000 stays the floor.
    hard_cap = max(6000, 4 * max_tokens)
    if max_answer_chars and max_answer_chars > 0:
        hard_cap = max_answer_chars
        max_tokens = max(64, min(600, max_answer_chars // 4 + 16))
        if structured_mode:
            # The cap governs the returned ANSWER, not the enumeration the
            # model needs room to write; the fold + truncation below enforce it.
            max_tokens = max(max_tokens, 900 if count_mode else 500)
            _notes.append(
                f" Keep the ANSWER line within about {max_answer_chars} "
                "characters; the ITEMS lines may be longer."
            )
        else:
            _notes.append(
                f" Keep the answer within about {max_answer_chars} "
                "characters — lead with the direct answer and drop anything not "
                "essential."
            )
    return max_tokens, hard_cap


def _checked_counts(text: str, question: str, *, count_mode: bool) -> str:
    """A count answer's recount and, for options, its threshold / statement /
    literal checks."""

    _LAST_COUNT_RECOMPUTED.set(False)
    _rc_override = _RECOUNT_OVERRIDE.get()
    _recount_on = True if _rc_override is None else bool(_rc_override)
    if count_mode and _recount_on:
        # The model lists well and counts badly; count its
        # own verdict lines and correct the ANSWER when they disagree.
        from server.memory.domain.ask_recount import recount

        rc = recount(text)
        if rc.recomputed:
            text = rc.text
            _LAST_COUNT_RECOMPUTED.set(True)
    if count_mode and _option_lines(question) >= 2:
        # The model lists values, the server
        # compares them against the question's threshold and picks the option.
        from server.memory.app.ask_literal_evidence import LAST_LITERAL_STATEMENTS
        from server.memory.domain.ask_threshold import verify_counts, verify_statement_count

        vst = verify_statement_count(text, question, int(LAST_LITERAL_STATEMENTS.get() or 0))
        if vst.changed:
            logger.info(
                "memory.ask: statement count → %s (%s user turn(s) carry the literal)",
                vst.letter,
                vst.inclusive,
            )
            text = vst.text
        vf = verify_counts(text, question) if not vst.changed else None
        if vf is not None and vf.changed:
            logger.info(
                "memory.ask: threshold verify → %s (%s reading; inclusive %s, strict %s)",
                vf.letter,
                vf.reading,
                vf.inclusive,
                vf.strict,
            )
            text = vf.text
        elif vf is not None:
            from server.memory.domain.ask_threshold import verify_literal_counts

            vl = verify_literal_counts(text, question)
            if vl.changed:
                logger.info(
                    "memory.ask: literal verify → %s (%s item(s) carry the literal)",
                    vl.letter,
                    vl.inclusive,
                )
                text = vl.text
    return text


def _reconciled_option(
    text: str, question: str, memories: list[Any], *, structured_mode: bool
) -> str:
    """The option letter checked against the question's option list and the
    evidence: an option named as unoffered, a missing letter, and for a
    single-valued question the latest statement, a comparison, arithmetic,
    a place description."""
    # The reader named an option it called unoffered: the evidence was
    # read right, the option list was not. Deterministic; see the module.
    from server.memory.domain.ask_option_guard import reconcile_unoffered_claim

    rec = reconcile_unoffered_claim(text, question)
    if rec.corrected:
        logger.info(
            "memory.ask: option letter %s → %s (answer named %r as unoffered; the "
            "option list offers it)",
            rec.from_letter,
            rec.to_letter,
            rec.named,
        )
        text = rec.text
    else:
        from server.memory.domain.ask_option_guard import reconcile_missing_letter

        rem = reconcile_missing_letter(text, question)
        if rem.corrected:
            logger.info(
                "memory.ask: option letter set to %s (the answer named %r without a letter)",
                rem.to_letter,
                rem.named,
            )
            text = rem.text
    if not structured_mode:
        # A single-valued question: the option stated latest across the
        # evidence clocks is the current one.
        from server.memory.domain.ask_latest_value import prefer_latest_statement

        lp = prefer_latest_statement(text, question, memories)
        if lp.changed:
            logger.info(
                "memory.ask: option letter %s → %s (latest statement wins; clocks %s)",
                lp.from_letter,
                lp.to_letter,
                lp.clocks,
            )
            text = lp.text

        # A comparison is decided by the two values the answer states,
        # not by the reader's sentence.
        from server.memory.domain.ask_compare import verify_comparison

        vc = verify_comparison(text, question)
        if vc.changed:
            logger.info(
                "memory.ask: comparison → %s (%s: %s)", vc.letter, vc.comparative, vc.values
            )
            text = vc.text

        # Letters in a name, digit sums: computed from the input the
        # answer states.
        from server.memory.domain.ask_arithmetic import verify_arithmetic

        va = verify_arithmetic(text, question)
        if va.changed:
            logger.info("memory.ask: arithmetic → %s (%s)", va.letter, va.detail)
            text = va.text

        # Place-description options: the
        # reader's own PLACES line maps each option to a city; the option
        # whose city is the memory's city is the answer.
        from server.memory.domain.ask_option_guard import verify_entity_location

        vloc = verify_entity_location(text, question, memories)
        if vloc.corrected:
            logger.info("memory.ask: entity location per the pool (%s)", vloc.named)
            text = vloc.text
        text = _reconcile_option_letters(text, question)
    return text


async def _synthesize(
    llm: Any,
    question: str,
    memories: list[Any],
    max_answer_chars: int = 0,
    *,
    hedge_ident: str = "",
    as_of: Any = None,
    stage: str = "ask_synthesize",
    intent: Any = None,
    omitted: list[Any] | None = None,
    graph_note: str = "",
    premise_system: str = "",
    raw_evidence: str = "",
    timeline: str = "",
    world_knowledge: bool = False,
    recent_block: str = "",
    presupposition: bool = False,
    record_note: bool = False,
    inference_note: bool = False,
    temporal_derive: bool = False,
    chain_note: bool = False,
    knowledge_block: str = "",
    scratchpad: str = "",
) -> str:
    from server.memory.domain.prompt_fence import FENCE_NOTE

    memories, excerpts, order_mode, nonce = _render_window(memories, intent, question)
    system = f"{_SYNTH_SYSTEM}{_ABSTAIN_CONSERVATIVE} {FENCE_NOTE}"
    excerpts, _notes = _context_notes(
        excerpts,
        question,
        memories,
        order_mode=order_mode,
        intent=intent,
        omitted=omitted,
        graph_note=graph_note,
        recent_block=recent_block,
        presupposition=presupposition,
        record_note=record_note,
        inference_note=inference_note,
        temporal_derive=temporal_derive,
        chain_note=chain_note,
        scratchpad=scratchpad,
        world_knowledge=world_knowledge,
        timeline=timeline,
        raw_evidence=raw_evidence,
        knowledge_block=knowledge_block,
        as_of=as_of,
        premise_system=premise_system,
        hedge_ident=hedge_ident,
    )
    count_mode, structured_mode = _answer_mode_notes(question, intent, _notes)
    max_tokens, hard_cap = _answer_budget(
        memories,
        max_answer_chars,
        count_mode=count_mode,
        structured_mode=structured_mode,
        notes=_notes,
    )
    # The premise verdict is a structured line the model
    # writes before the answer; code moves the refusal first when it says
    # unsupported. Rides the presupposition rule.
    from server.memory.domain import premise_line as _pl

    # A forced choice cannot be refused: with two or more options in the
    # question the premise line is off — an `unsupported`
    # verdict would put a refusal where the letter must be.
    from server.memory.domain.ask_option_guard import parse_options as _parse_opts
    from server.observability.genai import llm_stage

    _forced_choice = len(_parse_opts(question)) >= 2
    # Place-description options answer with a PLACES line too: give it its own tokens so it
    # cannot truncate the answer (measured: three answers cut mid-sentence at the 400-char
    # cap).
    if _forced_choice and any(len(v) > 40 for v in _parse_opts(question).values()):
        max_tokens += 60
    # An ungrounded identifier means the window never reached
    # the entity; a refusal-first opener would deny what a retrieval miss hid.
    premise_line_on = presupposition and not _forced_choice and not hedge_ident
    if premise_line_on:
        # The inference clause rides only the world_knowledge="allow" posture:
        # under "forbid" a refused inference is the stated posture.
        _notes.append(_pl.instruction(infer_ok=bool(world_knowledge)))
        max_tokens += 90

    # The window's attribution contrast, computed from
    # the rows' speaker and about:other tags, sits right under the question.
    _attribution = ""
    if presupposition:
        _attribution = _attr.attribution_note(question, memories)
    _attr.LAST_ATTRIBUTION.set("fired" if _attribution else "")
    _attribution_block = f"\n{_attribution}\n" if _attribution else ""
    # The layout and its block accounting.
    system, user = _assemble_prompt(
        system=system,
        excerpts=excerpts,
        question=question,
        attribution_block=_attribution_block,
        notes=_notes,
        raw_evidence=raw_evidence,
        memories=memories,
        nonce=nonce,
        parts=_context_block_chars(
            recent_block=recent_block,
            scratchpad=scratchpad,
            timeline=timeline,
            knowledge_block=knowledge_block,
            graph_note=graph_note,
            elision=elision_note(omitted or []),
        ),
    )
    _SYNTH_CALLS.set(int(_SYNTH_CALLS.get() or 0) + 1)
    # Labelled so the synthesis call is its own series, not llm.other.
    with llm_stage(stage), _window_keys_scope(memories):
        res = await llm.complete(system, user, max_tokens=max_tokens)
    # The model that actually answered. Differs from the client's nominal
    # model when a runtime fallback served the call (LOOM_LLM_FALLBACK_MODEL);
    # ask() reads it so synth_model on the wire is truthful.
    _LAST_SYNTH_MODEL.set(str(getattr(res, "model", "") or ""))
    text = str(res.text).strip()
    if premise_line_on:
        text, _pverdict, _psentence = _pl.split_premise(text)
        _pl.LAST_PREMISE.set(_pverdict)
        text, _ = _pl.apply(text, _pverdict, _psentence)
    else:
        _pl.LAST_PREMISE.set(None)
    text = _checked_counts(text, question, count_mode=count_mode)
    if structured_mode:
        text = fold_count_answer(text)
    if _option_lines(question) >= 2:
        text = _reconciled_option(text, question, memories, structured_mode=structured_mode)
    return text[:hard_cap]


async def _not_recorded_turns(
    search_input: Any,
    question: str,
    answer: str,
) -> tuple[str, str, int]:
    """When the first answer says the memories do not record the asked fact,
    recall the raw turns the question's words match for a second reading — on
    chat-history QA misses the fact often sat in a turn the extractor passed over or
    in a row the chunk wall kept out of the window. Returns (rendered turns,
    note, turn count); empty text when the fallback does not apply or
    nothing matched."""
    from server.memory.app import ask_when_evidence as _awe
    from server.settings import settings as _s

    is_when = _awe.is_when_question(question)
    is_advice = _awe.is_advice_request(question)
    hit = _awe.looks_not_recorded(answer)
    logger.info(
        "memory.ask: not-recorded fallback check: when=%s advice=%s not_recorded=%s",
        is_when,
        is_advice,
        hit,
    )
    if not hit:
        return "", "", 0
    turns = await _awe.fetch_dated_turns(
        org=getattr(search_input, "org", "") or "",
        namespace=getattr(search_input, "namespace", "") or "",
        agent=getattr(search_input, "agent", "") or "*",
        user_id=getattr(search_input, "user_id", None),
        question=question,
        top_k=int(_s.memory_ask_when_evidence_top_k),
        observation_date=getattr(search_input, "observation_date", None),
    )
    logger.info("memory.ask: not-recorded fallback recalled %d turn(s)", len(turns))
    if is_advice and not is_when:
        # An advice request that deferred is asked
        # again under the advice note, with or without turns — the request
        # itself is not a fact the turns need to state.
        return (_awe.render_turns(turns) if turns else ""), _awe.ADVICE_NOTE, len(turns)
    if not turns:
        return "", "", 0
    note = _awe.WHEN_EVIDENCE_NOTE if is_when else _awe.NOT_RECORDED_NOTE
    return _awe.render_turns(turns), note, len(turns)


async def _not_recorded_second(
    client: Any,
    question: str,
    memories: list[Any],
    answer_chars: int,
    kwargs: dict[str, Any],
    *,
    search_input: Any,
    answer: str,
) -> tuple[str, int]:
    """The second reading over the dated raw turns when the
    answer says the memories do not record the asked fact. Returns (second
    answer, turns read) — ("", 0) when the fallback does not apply, nothing
    was recalled, or the second reading came back empty. The synthesis kwargs
    are the first reading's with the note appended and the turns as the raw
    evidence."""
    raw, note, n = await _not_recorded_turns(search_input, question, answer)
    if not raw and not note:
        return "", 0
    kw = dict(kwargs)
    kw["premise_system"] = f"{kw.get('premise_system') or ''}{note}"
    if raw:
        kw["raw_evidence"] = raw
    second = await _synthesize(client, question, memories, answer_chars, **kw)
    return (second or ""), n


def _reconcile_option_letters(text: str, question: str) -> str:
    """The tail of the verifier chain: the option letter a stated relative
    date resolves to, the derived 'N more' figure leading
    the answer, and the place letter per the PLACES line."""
    from server.memory.domain.ask_option_guard import (
        lead_with_needed_figure,
        reconcile_place_options,
        verify_relative_date_option,
    )

    vr = verify_relative_date_option(text, question)
    if vr.corrected:
        logger.info(
            "memory.ask: option letter %s → %s (the stated date resolves to %r)",
            vr.from_letter,
            vr.to_letter,
            vr.named,
        )
        text = vr.text
    nm = lead_with_needed_figure(text, question)
    if nm.corrected:
        logger.info("memory.ask: 'need N more' answer now leads with %s", nm.named)
        text = nm.text
    rp = reconcile_place_options(text, question)
    if rp.corrected:
        logger.info(
            "memory.ask: option letter %s → %s (place %r per the PLACES line)",
            rp.from_letter,
            rp.to_letter,
            rp.named,
        )
    return rp.text


def _publish_window_keys(memories: list[Any]) -> list[Any]:
    """Every cached provider call of this ask (judge, selector,
    cheap and strong synthesis) stores the keys of the window it could have
    rendered, so a user erasure can delete the answer text by key. Returns
    the pool unchanged so the caller's assignment stays one line."""
    from server.llm.cache import set_window_keys

    set_window_keys(_keys_of(memories))
    return memories


def _window_keys_scope(memories: list[Any]) -> Any:
    """The cache row of one synthesis call carries exactly the
    keys it rendered — the block and the memories opened past it."""
    from server.llm.cache import window_keys_scope

    return window_keys_scope(_keys_of(memories))
