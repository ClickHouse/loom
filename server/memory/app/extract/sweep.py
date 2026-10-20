"""The coverage sweep: a second reading of the turns and statements the
first pass left without a memory, with its own prompts, batching and noise
filter.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any

from server.llm import customer
from server.llm.client import LLMResult, get_llm_client
from server.memory.app.extract.client import _merge_llm_results
from server.memory.app.extract.coverage import (
    sentence_is_listable,
    uncovered_sentences,
    uncovered_turn_indices,
)
from server.memory.app.extract.keys import _sanitize_key
from server.memory.app.extract.models import ExtractedFact, ExtractionError
from server.memory.app.extract.parse import _parse_fact_list
from server.memory.app.extract.turns import _format_indexed_turns
from server.observability.genai import generation_attrs, llm_stage
from server.settings import settings

log = logging.getLogger("server.memory.app.extract")


_SLIM_SWEEP_ROLE = """You are the second reading of a memory extractor. The first reading of
this conversation produced memories, listed under `already_extracted`. The
turns under `uncovered_turns` and the statements under `uncovered_sentences`
produced NO memory. Extract the concrete, durable things they state — an
event, a thing acquired, a place, an activity, a person, pet or object named,
a fact about a subject — as memories, one per statement, in the schema below.
Facts are about the speaker or the subject the statement names; the OTHER
speaker's news, questions, greetings, thanks and opinions are not facts.
Record a statement as it is asserted, even when it contradicts what you know,
and reuse the key of an earlier fact when a statement gives a new value for
the same attribute.

"""


def slim_sweep_system_prompt(system_prompt: str) -> str:
    """The sweep's own system prompt. The full
    extraction prompt is ~86k characters, three quarters of it worked
    examples for the FIRST reading; re-sending it would make every sweep call
    ~45k input tokens. The
    second reading needs the role, the output schema (taken verbatim from
    the live prompt so the two never drift) and the sweep addendum. Falls
    back to the full prompt when the schema header is not found. Pure."""
    i = system_prompt.find("OUTPUT SCHEMA")
    if i < 0:
        return system_prompt + _COVERAGE_SWEEP_ADDENDUM
    # The header line and its rule of '=' just above it.
    start = system_prompt.rfind("\n", 0, max(0, i - 1))
    start = system_prompt.rfind("\n", 0, start) + 1 if start > 0 else 0
    j = system_prompt.find("EXAMPLE", i)
    schema = system_prompt[start : j if j > 0 else len(system_prompt)].rstrip()
    return _SLIM_SWEEP_ROLE + schema + _COVERAGE_SWEEP_ADDENDUM


_COVERAGE_SWEEP_ADDENDUM = (
    "\n\n=============================================="
    "\nSECOND READING: TURNS THE FIRST PASS LEFT WITHOUT A MEMORY"
    "\n=============================================="
    "\nThe turns under `uncovered_turns` yielded NO memory on the first reading. "
    "Read each on its own and extract ONLY concrete, durable things the speaker "
    "states about THEMSELVES, even as an aside inside a reaction: an event they "
    "did or will do ('I just signed up for a pottery class yesterday'), a thing "
    "acquired ('Just got some new shoes, too!'), a place visited, an activity or "
    "hobby ('I'm off to go swimming'), a person, pet or object named, or a photo "
    "they shared and what it shows ('Here's a pic of my family camping at the "
    "beach' -> the family camps at the beach). "
    "Do NOT extract: questions the speaker asks, greetings, thanks, compliments, "
    "congratulations, agreement, encouragement, sympathy, restatements of the "
    "OTHER speaker's news, or general values and opinions ('family is "
    "everything', 'art is a great outlet'). Most of these turns hold nothing "
    "extractable; then emit nothing for them. "
    "Statements listed under `uncovered_sentences` are different: they sit inside "
    "turns you already read, and each one got NO memory. Extract EVERY one of them "
    "as its own fact with the subject, relation and object exactly as stated — "
    "including a statement that repeats the shape of an earlier one with a "
    "different value (that is an update, and the store keeps the newer value). "
    "Write each fact as a plain statement about its subject ('Thomas Arne was born "
    "in the city of Bengaluru'), never as 'User stated that …' — the entity is the "
    "subject, and the relation names it as subject. "
    "One fact per event. The uncovered turns were spoken on observation_date; "
    "resolve 'yesterday', 'last week' against THAT date only (the lines marked "
    "context are earlier turns, shown for pronouns). Cite the turn's index in "
    "source_message_indices. Do NOT repeat anything under `already_extracted`.\n"
)
# Conversational residue the sweep must not store even when the model emits
# it: the turn is about the exchange, not about the speaker.
_SWEEP_NOISE_RE = re.compile(
    r"\b(ask(s|ed)?|inquir(es|ed|y)|wonder(s|ed)?|question(s|ed)?|greet(s|ed)|"
    r"thank(s|ed)?|compliment(s|ed)?|congratulat(es|ed|ions?)|agree[sd]?|"
    r"acknowledg(es|ed)|encourag(es|ed)|reassur(es|ed)|sympathi[sz](es|ed)|"
    r"expressed (that|how|pride|support|sympathy|excitement|interest|gratitude)|"
    r"is (curious|interested) (about|in) what|wish(es|ed) .* (luck|well)|"
    r"emphasi[sz](es|ed) the importance|believes that|is glad that|is happy (for|that)|"
    r"looks forward to hearing|hopes (that )?\w+ (enjoys|has|will))\b",
    re.I,
)
_SWEEP_NOISE_KEY_RE = re.compile(
    r"(_question|_inquiry|_inquires|_greeting|_congratulations?|_compliment|_thanks|"
    r"_appreciation|_encouragement|_sympathy|_support_feedback|_reaction|_asks?_)$|_asks?_",
    re.I,
)


# Tag on every sweep fact: the pipeline stores them at a lower confidence so a
# first-pass fact wins a tie in ranking, and the tag is filterable.
_SWEEP_TAG = "coverage_sweep"


def sweep_fact_is_noise(f: ExtractedFact) -> bool:
    """Pure: a sweep fact about the exchange itself (a question asked, a
    compliment paid, a value voiced) rather than about the speaker's life."""
    if _SWEEP_NOISE_KEY_RE.search(f.memory_key):
        return True
    return bool(_SWEEP_NOISE_RE.search(f.value))


_LEAD_IN_RE = re.compile(
    r"^(?:the\s+)?user\s+(?:stated|said|mentioned|noted|reported)\s+that\s+", re.IGNORECASE
)


def strip_statement_lead_in(value: str) -> str:
    """ "User stated that the capital of Italy is Duluth." → "The capital of
    Italy is Duluth." Pure; a value without the lead-in is returned as is."""
    text = (value or "").strip()
    m = _LEAD_IN_RE.match(text)
    if not m:
        return text
    rest = text[m.end() :].strip()
    if len(rest) < 8:
        return text
    return rest[0].upper() + rest[1:]


def _sweep_user_prompt(
    messages: list[dict[str, object]],
    facts: list[ExtractedFact],
    idxs: list[int],
    sent_map: dict[int, list[str]],
    observation_date: str | None,
) -> str:
    """The sweep call's user prompt for one batch of listed items: the date,
    each listed turn's predecessor for pronouns, the first pass's facts so
    nothing is repeated, then the turns and the sentences. Pure."""
    parts: list[str] = []
    if observation_date:
        parts.append(
            f"observation_date: {observation_date} (the uncovered turns were spoken on this date)"
        )
    # Compact context: each uncovered turn's predecessor, not the whole
    # conversation — enough for a pronoun, a fraction of the first pass's
    # input. `last_messages` (prior session) is not shown: its dates mislead
    # the resolution of relative dates.
    ctx_idx = sorted({i - 1 for i in idxs if i >= 1} - set(idxs))
    if ctx_idx:
        parts.append(
            "context (earlier turns for pronouns only — do NOT extract from these):\n"
            + "\n".join(
                f"(context) {line}"
                for line in _format_indexed_turns(messages, ctx_idx).split("\n")
                if line
            )
        )
    if facts:
        parts.append(
            "already_extracted (do not repeat):\n"
            + "\n".join(f"  - {f.value.strip()[:200]}" for f in facts[:80])
        )
    parts.append("---")
    if idxs:
        parts.append("uncovered_turns (extract from these; cite the index shown):")
        parts.append(_format_indexed_turns(messages, idxs))
    if sent_map:
        parts.append(
            "uncovered_sentences (statements inside turns the first pass DID read, each "
            "of which got NO memory; extract each as its own fact and cite the turn index shown):"
        )
        for i in sorted(sent_map):
            for sent in sent_map[i]:
                parts.append(f"[{i}] {sent}")
    return "\n".join(parts)


def sweep_batches(
    idxs: list[int], sent_map: dict[int, list[str]], batch_items: int
) -> list[tuple[list[int], dict[int, list[str]]]]:
    """The listed items — turns first, then
    sentences in turn order — cut into groups of at most `batch_items`
    (0 = one group). Each group is one sweep call. Pure."""
    items: list[tuple[int, str | None]] = [(i, None) for i in idxs]
    items += [(i, s) for i in sorted(sent_map) for s in sent_map[i]]
    if batch_items <= 0 or len(items) <= batch_items:
        groups = [items]
    else:
        groups = [items[k : k + batch_items] for k in range(0, len(items), batch_items)]
    out: list[tuple[list[int], dict[int, list[str]]]] = []
    for group in groups:
        g_idxs = [i for i, s in group if s is None]
        g_sents: dict[int, list[str]] = {}
        for i, s in group:
            if s is not None:
                g_sents.setdefault(i, []).append(s)
        out.append((g_idxs, g_sents))
    return out


async def _coverage_sweep(
    messages: list[dict[str, object]],
    facts: list[ExtractedFact],
    *,
    system_prompt: str,
    observation_date: str | None,
    last_messages: list[dict[str, object]] | None,
    max_tokens: int,
    min_chars: int,
    max_turns: int,
    gate_reason: str = "",
    client: Any | None = None,
) -> tuple[list[ExtractedFact], LLMResult | None, int]:
    """One more extraction call over the turns the first pass
    cited nowhere. With `memory_extract_coverage_sweep_batch_items` > 0 the listed items are
    read in groups of that size, one call each, concurrently — one call
    over many items returns only a few facts whatever the prompt says.
    Returns the facts with
    the sweep's additions appended, the calls' summed usage (None when no
    call was made) and how many facts were added. A sweep fact must cite at
    least one listed turn; the rest are dropped (the addendum forbids
    repeating the first pass, and an uncited fact has no evidence to link).
    Fail-open: a provider error or an unparseable reply leaves the first
    pass's facts as they were (per batch, when batched)."""
    del last_messages  # the prior session is context the first pass had; see below
    idxs = uncovered_turn_indices(messages, facts, min_chars=min_chars)
    if max_turns > 0 and len(idxs) > max_turns:
        idxs = idxs[:max_turns]
    sent_map = uncovered_sentences(
        messages,
        facts,
        max_sentences=int(settings.memory_extract_coverage_sweep_max_sentences),
    )
    sent_map = {i: s for i, s in sent_map.items() if i not in set(idxs)}
    # A question, a greeting or a fragment cannot hold a fact.
    sent_map = {i: [x for x in sents if sentence_is_listable(x)] for i, sents in sent_map.items()}
    sent_map = {i: sents for i, sents in sent_map.items() if sents}
    if not idxs and not sent_map:
        return facts, None, 0
    batch_items = int(settings.memory_extract_coverage_sweep_batch_items)
    groups = sweep_batches(idxs, sent_map, batch_items)
    client = client if client is not None else get_llm_client()
    system = slim_sweep_system_prompt(system_prompt)
    sem = asyncio.Semaphore(4)

    async def _one(g_idxs: list[int], g_sents: dict[int, list[str]]) -> LLMResult | None:
        user_prompt = _sweep_user_prompt(messages, facts, g_idxs, g_sents, observation_date)
        try:
            async with sem:
                with (
                    llm_stage("extract_coverage_sweep"),
                    generation_attrs(
                        sweep_gate=gate_reason,
                        sweep_prompt="slim",
                        sweep_batches=str(len(groups)),
                    ),
                ):
                    return await client.complete(
                        system=system,
                        user=user_prompt,
                        max_tokens=max_tokens,
                        response_format={"type": "json_object"},
                    )
        except customer.HOLDS:
            raise  # a held call is not a provider failure: the extract waits
        except Exception as e:  # noqa: BLE001 — the first pass's facts stand
            log.warning("coverage sweep call failed (non-fatal): %s", e)
            return None

    if len(groups) == 1:
        results = [await _one(*groups[0])]
    else:
        results = list(await asyncio.gather(*(_one(g_idxs, g_sents) for g_idxs, g_sents in groups)))
    swept: list[ExtractedFact] = []
    result: LLMResult | None = None
    for r in results:
        if r is None:
            continue
        try:
            swept.extend(_parse_fact_list(r.text, discards=[]))
        except ExtractionError as e:
            log.warning("coverage sweep reply unparseable (non-fatal): %s", e)
        result = (
            r if result is None else _merge_llm_results(result, r, text=result.text + "\n" + r.text)
        )
    if result is None:
        return facts, None, 0
    allowed = set(idxs) | set(sent_map)
    existing_keys = {f.memory_key for f in facts}
    added: list[ExtractedFact] = []
    for f in swept:
        if not any(i in allowed for i in (f.source_message_indices or [])):
            continue
        if sweep_fact_is_noise(f):
            continue
        tags = list(f.tags)
        if _SWEEP_TAG not in tags:
            tags.append(_SWEEP_TAG)
        # The model still leads some facts with "User stated that …" whatever
        # the prompt says; the lead-in is provenance (the sweep tag carries
        # it), not content, and it keeps the row from folding with the
        # first-pass row it updates ("The capital of Italy is Rome" /
        # "User stated that the capital of Italy is Duluth" would both stay
        # active).
        f = replace(f, tags=tags, value=strip_statement_lead_in(f.value))
        if f.memory_key in existing_keys:
            # Same key as a first-pass fact: the store would supersede one
            # with the other. Keep the sweep's row under a turn-distinct key
            # so an event repeated across turns stays a separate memory.
            f = replace(
                f, memory_key=_sanitize_key(f"{f.memory_key}_t{min(f.source_message_indices)}")
            )
        existing_keys.add(f.memory_key)
        added.append(f)
    if added:
        log.info(
            "coverage sweep added %d fact(s) from %d uncovered turn(s) and %d sentence(s) in %d call(s)",
            len(added),
            len(idxs),
            sum(len(v) for v in sent_map.values()),
            len(groups),
        )
    return list(facts) + added, result, len(added)
