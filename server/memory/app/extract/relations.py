"""The relation backfill: one small call for the typed triples of the facts
the first pass left without any.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from server.llm import customer
from server.llm.client import LLMResult, get_llm_client
from server.llm.json_text import strip_code_fence
from server.memory.app.extract.models import ExtractedFact
from server.observability.genai import llm_stage

log = logging.getLogger("server.memory.app.extract")


_RELATION_BACKFILL_SYSTEM_PROMPT = """You turn short factual sentences into typed triples.

You are given numbered facts, each with the entities it mentions. For each
fact return ONE triple {"i": <fact number>, "s": subject, "v": predicate,
"o": object}:
  - "s" is the entity the sentence is about, "o" the entity or value stated
    about it, both copied from the sentence in lowercase.
  - "v" is a short lowercase snake_case predicate naming the attribute, the
    same word for the same attribute every time: ceo_of, chairperson_of,
    director_of, author_of, founded_by, born_in, died_in, located_in,
    married_to, citizen_of, plays_position, associated_with_sport,
    headquartered_in, educated_at, famous_for, speaks_language, child_of,
    parent_of, performed_by, produced_by, developed_by, broadcast_by, religion.
  - Social relationships take the relationship as the predicate, always the
    same word for the same kind of tie: sibling_of (any sister or brother),
    friend_of, colleague_of, neighbour_of, partner_of, mentor_of, student_of,
    classmate_of, roommate_of, cousin_of, works_with. "Y's elder sister is Z"
    → s = y, v = sibling_of, o = z; "Z is Y's mentor" → s = z, v = mentor_of,
    o = y.
  - "The X of Y is Z" → s = y, v = x_of, o = z. "Y was Xed by Z" → s = y,
    v = xed_by, o = z. "Y Xs Z" → s = y, v = x, o = z.
Skip a fact that states no relationship between two things. Return exactly
one JSON object: {"relations": [...]}. No prose."""


async def _backfill_relations(
    facts: list[ExtractedFact],
    *,
    max_tokens: int = 2048,
    client: Any | None = None,
) -> tuple[list[ExtractedFact], LLMResult | None, int]:
    """One small call for the typed relations of the facts the
    first pass left without any. The `relations` field of the extraction
    output is all-or-nothing per batch — the model either writes a triple on
    every fact or on none — and the typed-triple plane (facts chain,
    interval repair, supersession) is only as good as that field.

    Only facts with two or more entities and no relation are sent, with
    their values and entities; the reply's triples are canonicalised like
    the first pass's. Returns the facts (same order), the call's usage (None
    when no call was made) and how many facts gained a relation. Fail-open:
    a parse failure or a provider error leaves the facts as they were."""
    todo = [i for i, f in enumerate(facts) if len(f.entities) >= 2 and not f.relations]
    if not todo:
        return facts, None, 0
    lines = []
    for n, i in enumerate(todo):
        f = facts[i]
        ents = ", ".join(f.entities[:6])
        lines.append(f"{n}. {f.value.strip()[:300]}  [entities: {ents}]")
    user_prompt = (
        "Facts:\n" + "\n".join(lines) + '\n\nReturn the JSON {"relations": [...]} object now.'
    )
    client = client if client is not None else get_llm_client()
    try:
        with llm_stage("extract_relations"):
            result = await client.complete(
                system=_RELATION_BACKFILL_SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
    except customer.HOLDS:
        raise  # a held call is not a provider failure: the extract waits
    except Exception as e:  # noqa: BLE001 — the first pass's facts stand
        log.warning("relation backfill call failed (non-fatal): %s", e)
        return facts, None, 0
    try:
        candidate = strip_code_fence(result.text)
        start, end = candidate.find("{"), candidate.rfind("}")
        payload = json.loads(candidate[start : end + 1]) if start != -1 and end != -1 else {}
        raw = payload.get("relations", []) if isinstance(payload, dict) else []
    except (json.JSONDecodeError, ValueError):
        raw = []
    if not isinstance(raw, list):
        raw = []
    from server.memory.domain.search_text import canonicalize_verb

    gained: dict[int, list[tuple[str, str, str]]] = {}
    for rel in raw:
        if not isinstance(rel, dict):
            continue
        try:
            n = int(rel.get("i", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= n < len(todo):
            continue
        s_ = str(rel.get("s", "")).strip().lower()
        v_ = canonicalize_verb(str(rel.get("v", "")))
        o_ = str(rel.get("o", "")).strip().lower()
        if len(s_) < 2 or len(v_) < 2 or len(o_) < 1:
            continue
        bucket = gained.setdefault(todo[n], [])
        if len(bucket) < 4 and (s_, v_, o_) not in bucket:
            bucket.append((s_, v_, o_))
    if not gained:
        return facts, result, 0
    out = list(facts)
    for i, rels in gained.items():
        out[i] = replace(out[i], relations=rels)
    return out, result, len(gained)
