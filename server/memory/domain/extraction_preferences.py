"""A stated preference is a fact of its own.

"Besides great views, I also like hotels with unique features, such as a rooftop pool or
a hot tub on the balcony" (said while booking one Seattle hotel) came out of extraction
as three rows about The Edgewater Hotel and none about what the user likes in a hotel.
The next question — "suggest a hotel for my trip to Miami" — is judged on whether the
answer uses that taste, and the store had only the booking. Extraction distils a turn
into what happened; a taste stated in passing is the part that carries to the next
occasion.

The recovery is deterministic. A user turn's sentence that states a
preference in the first person ("I like / love / prefer / enjoy / am a fan
of / dislike / hate / avoid / always … / never …") with an object of at
least two content words, and no extracted fact from that turn sharing at
least half of the object's content words, yields one fact: the sentence
rewritten to the third person under a `user_preference_<slug>` key, tagged
`preference`, cited to the turn. Questions, hypotheticals and requests
("I'd like you to", "I like to know") are not preferences.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TOKEN_RE = re.compile(r"[a-z]{3,}")
_STOP = frozenset(
    "the and for with that this from his her their its was were been have has had got get about "
    "just really very some they them she him you your our who what when which into onto also "
    "such like love prefer enjoy hate dislike avoid always never usually generally tend fan "
    "big".split()
)
_PREF_RE = re.compile(
    r"\bI(?:'m| am)? (?:also |really |generally |usually |personally |definitely |absolutely )?"
    r"(?P<verb>like|love|prefer|enjoy|adore|am a (?:big |huge )?fan of|(?:really )?dislike|hate|"
    r"can't stand|cannot stand|avoid|always (?:go for|choose|pick|order|get)|"
    r"never (?:go for|choose|pick|order|get))\s+(?P<obj>[^.!?;,]{6,160})",
    re.IGNORECASE,
)
_NOT_PREF_RE = re.compile(
    r"\bI(?:'d| would) like\b|\bI like to know\b|\bI like how\b|\bwould (?:you|it)\b|\?",
    re.IGNORECASE,
)
_VERB_3P = {
    "like": "likes",
    "love": "loves",
    "prefer": "prefers",
    "enjoy": "enjoys",
    "adore": "adores",
    "dislike": "dislikes",
    "really dislike": "dislikes",
    "hate": "hates",
    "can't stand": "cannot stand",
    "cannot stand": "cannot stand",
    "avoid": "avoids",
}


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP}


def _slug(text: str, limit: int = 40) -> str:
    words = [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP][:5]
    return "_".join(words)[:limit].strip("_") or "stated"


def _third_person(verb: str, obj: str) -> str:
    v = verb.lower()
    if v.startswith("am a"):
        v3 = "is a fan of"
    elif v.startswith("always ") or v.startswith("never "):
        adv, _, base = v.partition(" ")
        v3 = f"{adv} {base}s" if not base.endswith("s") else f"{adv} {base}"
    else:
        v3 = _VERB_3P.get(v, v)
    return f"User {v3} {obj.strip()}."


def stated_preferences(text: str) -> list[tuple[str, str, str]]:
    """(sentence, verb, object) for each first-person preference the text
    states; an empty list when it states none."""
    out: list[tuple[str, str, str]] = []
    for sent in _SENTENCE_SPLIT_RE.split(text or ""):
        if _NOT_PREF_RE.search(sent):
            continue
        for m in _PREF_RE.finditer(sent):
            obj = m.group("obj").strip()
            if len(_tokens(obj)) >= 2:
                out.append((sent, m.group("verb"), obj))
    return out


def recover_stated_preferences(
    facts: Sequence[Any],
    messages: Sequence[Mapping[str, Any]] | None,
    make: Any,
    *,
    min_share: float = 0.5,
    max_new: int = 4,
) -> list[Any]:
    """`facts` plus one new fact (built by `make(memory_key, value, index)`)
    for each stated preference no extracted fact from the same turn already
    carries. Unchanged facts are returned as the same objects."""
    if not messages:
        return list(facts)
    out = list(facts)
    added = 0
    for idx, m in enumerate(messages):
        if str(m.get("role") or "user").lower() not in ("user", "human", ""):
            continue
        prefs = stated_preferences(str(m.get("content") or ""))
        if not prefs:
            continue
        turn_facts = [
            f for f in facts if idx in (getattr(f, "source_message_indices", None) or [])
        ] or list(facts)
        covered = [_tokens(str(getattr(f, "value", "") or "")) for f in turn_facts]
        for _sent, verb, obj in prefs:
            ot = _tokens(obj)
            if not ot or any(len(ot & c) / len(ot) >= min_share for c in covered):
                continue
            key = f"user_preference_{_slug(obj)}"
            if any(getattr(f, "memory_key", "") == key for f in out):
                continue
            out.append(make(key, _third_person(verb, obj), idx))
            covered.append(ot)
            added += 1
            if added >= max_new:
                return out
    return out
