"""Who said it: speaker tags from the cited turns' names, and facts about
items only the assistant introduced re-attributed to the assistant.
"""

from __future__ import annotations

import re
from dataclasses import replace

from server.memory.app.extract.models import ExtractedFact
from server.memory.app.extract.turns import _message_speaker


def apply_message_speakers(
    facts: list[ExtractedFact],
    messages: list[dict[str, object]],
) -> list[ExtractedFact]:
    """Deterministic speaker attribution: a fact whose cited
    source turns all come from ONE named speaker gets `speaker:<name>` as a
    tag and the lowercased name as an entity, so "according to X" reads can
    filter and so the graph knows who said it. Facts cited to two speakers
    (an exchange) or to unnamed turns are untouched."""
    if not facts or not messages:
        return facts
    names = [_message_speaker(m) for m in messages]
    if not any(names):
        return facts
    out: list[ExtractedFact] = []
    changed = False
    for f in facts:
        idx = [i for i in (f.source_message_indices or []) if 0 <= i < len(names)]
        cited = {names[i] for i in idx}
        cited.discard("")
        if len(cited) != 1:
            out.append(f)
            continue
        low = next(iter(cited)).lower()
        tag = f"speaker:{low}"
        tags = list(f.tags)
        entities = list(f.entities)
        if tag not in tags:
            tags.append(tag)
        if low not in (e.lower() for e in entities):
            entities.append(low)
        if tags != f.tags or entities != f.entities:
            out.append(replace(f, tags=tags, entities=entities))
            changed = True
        else:
            out.append(f)
    return out if changed else facts


_TITLE_RE = re.compile(
    r"\b([A-Z][\w'’&:-]*"
    r"(?:\s+(?:(?:of|the|and|in|on|a|an|to|for|&)\s+)?[A-Z][\w'’&:-]*){0,5}"
    r"(?:\s*\(\d{4}\))?)"
)
_TITLE_STOP = frozenset(
    {
        "User",
        "Assistant",
        "The",
        "A",
        "An",
        "I",
        "He",
        "She",
        "They",
        "It",
        "This",
        "That",
        "User's",
        "Users",
    }
)
_USER_LEAD_RE = re.compile(r"^\s*(the\s+)?user\b", re.IGNORECASE)


def _candidate_items(value: str) -> list[str]:
    """Title-cased spans in a fact value that could name a work, dish, product
    or place ("Good Will Hunting (1997)", "Aged Cheddar", "The Cases That
    Haunt Us"). Single common words and speaker labels are excluded."""
    out: list[str] = []
    for m in _TITLE_RE.finditer(value or ""):
        span = m.group(1).strip()
        words = [w for w in re.split(r"\s+", span) if w]
        if not words or words[0] in _TITLE_STOP:
            words = words[1:]
        span = " ".join(words).strip()
        if len(span) < 4 or (len(words) < 2 and not re.search(r"\(\d{4}\)|\d", span)):
            continue
        if span not in out:
            out.append(span)
    return out


def attribute_assistant_introduced(
    facts: list[ExtractedFact], messages: list[dict[str, object]]
) -> list[ExtractedFact]:
    """Re-attribute facts about items only the assistant named.

    The extractor sometimes writes an assistant's endorsement ("I'm all about
    Aged Cheddar") as the user's interest ("User is interested in Aged
    Cheddar"). Speaker is recoverable from the evidence without a model: if an
    item named in the fact occurs in assistant turns and in no user turn, the
    assistant introduced it. Such a fact is rewritten in the assistant's voice,
    keyed `assistant_...`, and tagged `speaker:assistant`; every other fact is
    returned unchanged. Never drops a fact."""
    if not facts or not messages:
        return facts
    user_text = " ".join(
        str(m.get("content") or "") for m in messages if str(m.get("role") or "") != "assistant"
    ).lower()
    asst_text = " ".join(
        str(m.get("content") or "") for m in messages if str(m.get("role") or "") == "assistant"
    ).lower()
    if not asst_text:
        return facts
    out: list[ExtractedFact] = []
    for f in facts:
        if f.memory_key.startswith("assistant_") or not _USER_LEAD_RE.match(f.value):
            out.append(f)
            continue
        introduced = [
            it
            for it in _candidate_items(f.value)
            if it.lower() in asst_text and it.lower() not in user_text
        ]
        if not introduced:
            out.append(f)
            continue
        item = introduced[0]
        value = f"Assistant recommended {item}. " + re.sub(
            r"^\s*(the\s+)?user('s)?\b",
            lambda m: "The user's" if m.group(2) else "The user",
            f.value,
            count=1,
            flags=re.IGNORECASE,
        )
        key = "assistant_" + f.memory_key.removeprefix("user_")
        tags = list(f.tags) + (["speaker:assistant"] if "speaker:assistant" not in f.tags else [])
        out.append(replace(f, memory_key=key, value=value, tags=tags[:6]))
    return out
