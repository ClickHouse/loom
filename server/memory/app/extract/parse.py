"""Parsing the extractor's JSON reply into facts, and recognising and
salvaging a reply the output budget cut off.
"""

from __future__ import annotations

import json
import re
from typing import Any

from server.llm.json_text import strip_code_fence
from server.memory.app.extract.keys import _sanitize_key
from server.memory.app.extract.models import (
    MAX_DERIVED_PER_FACT,
    ExtractedFact,
    ExtractionError,
)
from server.memory.domain import temporal_confidence as _temporal_confidence


def _parse_fact_list(
    text: str,
    *,
    discards: list[str] | None = None,
) -> list[ExtractedFact]:
    """The LLM may wrap JSON in markdown fences or add a preamble — strip
    those defensively before parsing.

    `discards`, when given, receives one short reason per fact object the
    parser dropped (non-dict, or missing key/value) — the caller counts
    them as `dropped_parse` instead of losing them silently.
    """
    candidate = strip_code_fence(text)
    # First { to last } — most permissive JSON extraction.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ExtractionError(f"no JSON object in LLM response: {text[:200]!r}")
    try:
        payload = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as e:
        raise ExtractionError(f"invalid JSON in LLM response: {e}") from e
    raw_facts = payload.get("facts", [])
    if not isinstance(raw_facts, list):
        raise ExtractionError(
            f"expected 'facts' to be a list, got {type(raw_facts).__name__}",
        )
    facts: list[ExtractedFact] = []
    for raw in raw_facts:
        if not isinstance(raw, dict):
            if discards is not None:
                discards.append(f"not_an_object:{type(raw).__name__}")
            continue
        key = str(raw.get("key", "")).strip()
        value = str(raw.get("value", "")).strip()
        if not key or not value:
            if discards is not None:
                discards.append(f"missing_{'key' if not key else 'value'}:{key or '?'}")
            continue
        raw_tags = raw.get("tags", [])
        if isinstance(raw_tags, list):
            tags = [str(t).strip().lower() for t in raw_tags if isinstance(t, (str, int))][:4]
        else:
            tags = []
        raw_entities = raw.get("entities", [])
        if isinstance(raw_entities, list):
            entities = [
                str(e).strip().lower()
                for e in raw_entities
                if isinstance(e, (str, int)) and str(e).strip()
            ][:6]
            # Dedup preserving order — LLMs sometimes repeat names.
            entities = list(dict.fromkeys(entities))
        else:
            entities = []
        topics = _parse_topics(raw.get("topics", []), entities)
        raw_linked = raw.get("linked_keys", [])
        # The prompt instructs the LLM to link by integer index; the caller
        # resolves "__idx:N" against existing_memories post-parse. A digit may
        # arrive JSON-serialized as a string ("3"), so accept that too.
        linked_keys: list[str] = []
        if isinstance(raw_linked, list):
            for lk in raw_linked[:3]:
                if isinstance(lk, bool):
                    continue
                if isinstance(lk, int):
                    linked_keys.append(f"__idx:{lk}")
                elif isinstance(lk, str) and lk.strip().isdigit():
                    linked_keys.append(f"__idx:{int(lk.strip())}")
        raw_sources = raw.get("source_message_indices", [])
        source_message_indices: list[int] = []
        if isinstance(raw_sources, list):
            seen_source_indices: set[int] = set()
            for idx_raw in raw_sources[:3]:
                try:
                    idx = int(idx_raw)
                except (TypeError, ValueError):
                    continue
                if idx < 0 or idx in seen_source_indices:
                    continue
                seen_source_indices.add(idx)
                source_message_indices.append(idx)
        # ISO shape only (YYYY-MM-DD[THH:MM:SS]); anything else → None.
        # Date-only is padded to a full datetime for CH compat.
        raw_anchor = raw.get("temporal_anchor")
        temporal_anchor: str | None = None
        if isinstance(raw_anchor, str) and raw_anchor.strip():
            anchor_str = raw_anchor.strip()[:19]
            if re.match(r"^\d{4}-\d{2}-\d{2}", anchor_str):
                if len(anchor_str) == 10:
                    anchor_str = anchor_str + "T00:00:00"
                temporal_anchor = anchor_str
        temporal_confidence = _temporal_confidence.coerce(
            raw.get("temporal_confidence"), has_anchor=temporal_anchor is not None
        )
        raw_valid = raw.get("valid_at")
        valid_at: str | None = None
        if isinstance(raw_valid, str) and raw_valid.strip():
            valid_str = raw_valid.strip()[:19]
            if re.match(r"^\d{4}-\d{2}-\d{2}", valid_str):
                if len(valid_str) == 10:
                    valid_str = valid_str + "T00:00:00"
                valid_at = valid_str
        raw_valid_to = raw.get("valid_to")
        valid_to: str | None = None
        if isinstance(raw_valid_to, str) and raw_valid_to.strip():
            vt_str = raw_valid_to.strip()[:19]
            if re.match(r"^\d{4}-\d{2}-\d{2}", vt_str):
                if len(vt_str) == 10:
                    vt_str = vt_str + "T00:00:00"
                valid_to = vt_str

        # Bounded to a short phrase so it can't smuggle the whole
        # transcript into the embed text. Empty / non-string → "".
        raw_topic = raw.get("question_topic")
        raw_evidence = raw.get("evidence")
        evidence = str(raw_evidence).strip()[:400] if isinstance(raw_evidence, str) else ""
        question_topic = ""
        if isinstance(raw_topic, str) and raw_topic.strip():
            question_topic = " ".join(raw_topic.split())[:120]

        raw_relations = raw.get("relations", [])
        relations: list[tuple[str, str, str]] = []
        relation_phrases: list[str] = []
        if isinstance(raw_relations, list):
            from server.memory.domain.search_text import canonicalize_verb, verb_phrase

            for rel in raw_relations[:4]:
                if not isinstance(rel, dict):
                    continue
                s = str(rel.get("s", "")).strip().lower()
                v = canonicalize_verb(str(rel.get("v", "")))
                o = str(rel.get("o", "")).strip().lower()
                if not s or not v or not o:
                    continue
                if len(s) < 2 or len(v) < 2 or len(o) < 1:
                    continue
                relations.append((s, v, o))
                relation_phrases.append(verb_phrase(str(rel.get("v", ""))) or v)

        # Per-occurrence operands for cross-session count/sum/date-diff;
        # canonical predicate so occurrences group.
        raw_derived = raw.get("derived", [])
        derived: list[dict[str, object]] = []
        if isinstance(raw_derived, list):
            for d in raw_derived[:MAX_DERIVED_PER_FACT]:
                if not isinstance(d, dict):
                    continue
                obj = str(d.get("object", "")).strip()
                pred = canonicalize_verb(str(d.get("predicate", "")))
                if not obj or not pred:
                    continue
                nv = d.get("numeric_value")
                try:
                    nv = float(nv) if nv is not None and str(nv).strip() != "" else None
                except (TypeError, ValueError):
                    nv = None
                occ = str(d.get("occurred_at", "")).strip() or (temporal_anchor or "")
                derived.append(
                    {
                        "predicate": pred,
                        "object": obj[:200],
                        "category": str(d.get("category", "")).strip().lower()[:80],
                        "numeric_value": nv,
                        "unit": str(d.get("unit", "")).strip().lower()[:24],
                        "occurred_at": occ,
                    }
                )
        # Foresight window, carried only when it is an object.
        _raw_fs = raw.get("foresight")
        _foresight: dict[str, Any] | None = dict(_raw_fs) if isinstance(_raw_fs, dict) else None
        facts.append(
            ExtractedFact(
                memory_key=_sanitize_key(key),
                value=value,
                tags=tags,
                entities=entities,
                linked_keys=linked_keys,
                source_message_indices=source_message_indices,
                relations=relations,
                relation_phrases=relation_phrases,
                derived=derived,
                temporal_anchor=temporal_anchor,
                temporal_confidence=temporal_confidence,
                valid_at=valid_at,
                valid_to=valid_to,
                foresight=_foresight,
                about=_parse_about(raw.get("about")),
                forget=raw.get("forget") is True,
                question_topic=question_topic,
                topics=topics,
                evidence=evidence,
            )
        )
    return facts


def _parse_about(raw_about: object) -> str:
    """Whose fact it is: "other", "agent" (a rule the assistant states about
    itself) or "user" for anything else."""
    about = str(raw_about or "").strip().lower()
    return about if about in ("other", "agent") else "user"


_MAX_TOPICS = 3
_MAX_TOPIC_CHARS = 80


def _parse_topics(raw_topics: object, entities: list[str]) -> list[str]:
    """Concept phrases from the extractor: lowercase, trimmed, deduped, at
    most _MAX_TOPICS, never a bare copy of an entity (that is what the
    entities field is for) and never a single stopword-length token."""
    if not isinstance(raw_topics, list):
        return []
    ents = {e.lower() for e in entities}
    out: list[str] = []
    seen: set[str] = set()
    for t in raw_topics:
        if not isinstance(t, (str, int)):
            continue
        text = " ".join(str(t).strip().lower().split())[:_MAX_TOPIC_CHARS].strip(" .,;:")
        if len(text) < 3 or text in ents or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= _MAX_TOPICS:
            break
    return out


# Ceiling for the enlarged-budget truncation retry: 2x the caller's
# budget, never above this. Bounds worst-case spend on a pathological
# transcript while clearing the common "long session, default budget
# was not enough" case.
_TRUNCATION_RETRY_MAX_TOKENS = 8192


def _looks_truncated(text: str) -> bool:
    """Heuristic truncation detector for a JSON-object response: the text
    opens a JSON object but its braces/brackets never re-balance. A parse
    failure of this shape means max_tokens cut the output mid-stream — a
    same-budget retry would truncate at the same place."""
    candidate = (text or "").strip().strip("`")
    start = candidate.find("{")
    if start == -1:
        return False
    body = candidate[start:]
    return body.count("{") > body.count("}") or body.count("[") > body.count("]")


def _salvage_fact_objects(text: str) -> list[dict[str, object]] | None:
    """Recover the complete fact objects from a truncated extraction
    response. Walks the `"facts"` array decoding one object at a time and
    stops at the first incomplete one — a partial trailing object is
    dropped, never completed by guesswork. Returns None when no complete
    object exists (caller falls through to the hard failure)."""
    candidate = strip_code_fence(text)
    key = candidate.find('"facts"')
    if key == -1:
        return None
    arr = candidate.find("[", key)
    if arr == -1:
        return None
    decoder = json.JSONDecoder()
    objs: list[dict[str, object]] = []
    i = arr + 1
    n = len(candidate)
    while i < n:
        while i < n and candidate[i] in " \t\r\n,":
            i += 1
        if i >= n or candidate[i] != "{":
            break
        try:
            obj, end = decoder.raw_decode(candidate, i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            objs.append(obj)
        i = end
    return objs or None
