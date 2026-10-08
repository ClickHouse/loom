"""The attribution note: who the window's facts are
about, computed from the rows and placed beside the question.

The 335 speaker-swap adversarial items of a conversation QA bench name the partner of
the person who said the fact ("How does Deborah describe her snakes?" when Jolene
did). With the speaker on every context line and an instruction to check it, a small
model still wrote `Premise: … — supported` and answered for Deborah: the rule sat at
the end of a long system prompt while the question's framing sat in front of the
memories. The rows already carry the
answer — `speaker:<name>` and `about:other` — so the contrast is computed here and
written in one sentence right under the question, where the model reads first.

The note fires only when the question names exactly one speaker seen in
the window and, among the fact rows that share a topic word with the
question, none is that speaker talking or another speaker talking about
them while at least one is another speaker talking about their own life.
A question about what the named person did, evidenced by the partner's
`about:other` row, never fires it. Pure; no LLM.
"""

from __future__ import annotations

import re
from collections import Counter
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

# "fired" when the last synthesis call carried the note, "" otherwise.
LAST_ATTRIBUTION: ContextVar[str] = ContextVar("loom_ask_attribution", default="")

# A dialogue line inside a chunk excerpt: "[user] Deborah: …" or a line
# starting "Deborah: …". Gives the roster of speakers when the fact rows
# name only one of them.
_CHUNK_LINE_RE = re.compile(r"(?:^|\n|\]\s+)([A-Z][a-zA-Z'\-]+):\s")
# Topic tokens: four letters or more, minus function words and speaker
# names. A fact row is on the question's topic when it shares one.
_TOKEN_RE = re.compile(r"[a-z]{4,}")
_STOP = frozenset(
    "the a an of and to in on at for with her his their she he they what when where which "
    "who how does did do is was were are be been about from that this it its as by have has "
    "had will would could should there these those into over than then them some more most "
    "also very just like your you".split()
)


def _stem(t: str) -> str:
    # Light plural fold so "snakes" meets "snake" and "classes" meets "class".
    if len(t) > 5 and t.endswith(("ses", "xes", "zes", "ches", "shes")):
        return t[:-2]
    if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def topic_tokens(text: str, names: set[str]) -> set[str]:
    lowered = {n.lower() for n in names}
    return {
        _stem(t)
        for t in _TOKEN_RE.findall((text or "").lower())
        if t not in _STOP and t not in lowered
    }


def _cap(name: str) -> str:
    n = (name or "").strip()
    return n[:1].upper() + n[1:] if n else n


def _speaker(m: Any) -> str:
    return _cap(str(getattr(m, "speaker", "") or ""))


def _is_chunk(m: Any) -> bool:
    key = str(getattr(m, "memory_key", "") or "")
    return key.startswith("chunk:") or str(getattr(m, "source", "") or "") == "chunk"


def roster(memories: list[Any]) -> set[str]:
    """Speakers seen in the window: the fact rows' speaker tags plus the
    names that open dialogue lines in chunk excerpts."""
    out: set[str] = set()
    for m in memories:
        sp = _speaker(m)
        if sp:
            out.add(sp)
        if _is_chunk(m):
            for name in _CHUNK_LINE_RE.findall(str(getattr(m, "content_excerpt", "") or "")):
                out.add(_cap(name))
    return out


@lru_cache(maxsize=1024)
def _whole_word(name: str) -> re.Pattern[str]:
    """The compiled whole-word, case-blind pattern for one speaker name. Names
    come from row text (any `Capitalised:` line start), so the cache is bounded."""
    return re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)


def named_speaker(question: str, names: set[str]) -> str:
    """The one roster name the question mentions as a whole word; "" when
    none or more than one."""
    found: list[str] = []
    for name in sorted(names):
        if _whole_word(name).search(question or ""):
            found.append(name)
    return found[0] if len(found) == 1 else ""


def attribution_note(question: str, memories: list[Any]) -> str:
    """One sentence for the user message, or "" when the window does not
    make the contrast."""
    if not question or not memories:
        return ""
    names = roster(memories)
    named = named_speaker(question, names)
    if not named:
        return ""
    # Only the rows on the question's topic count: the window nearly always
    # holds some unrelated row by the named person (their job, their pet),
    # and over the whole window the note fired on 7 of 2,065 asks. On the
    # topical rows it fires on 110 of 332 speaker swaps and 14 of 1,540
    # answerable questions (simulated on the retrieved windows).
    qt = topic_tokens(question, names)
    if not qt:
        return ""
    facts = [
        m
        for m in memories
        if _speaker(m)
        and not _is_chunk(m)
        and topic_tokens(str(getattr(m, "content_excerpt", "") or ""), names) & qt
    ]
    if not facts:
        return ""
    about_named = 0
    others: Counter[str] = Counter()
    for m in facts:
        sp = _speaker(m)
        about_other = bool(getattr(m, "about_other", False))
        if (sp == named and not about_other) or (sp != named and about_other):
            about_named += 1
        elif sp != named:
            others[sp] += 1
    if about_named or not others:
        return ""
    other, n = others.most_common(1)[0]
    rows = "row is" if n == 1 else "rows are"
    rows = rows.replace(" ", " on this topic ")
    return (
        f"Attribution: the question is about {named}. None of the memories below is "
        f"{named} speaking, nor another speaker speaking about {named}; the {n} fact "
        f"{rows} {other} speaking about {other}'s own life. A premise "
        f"that gives them to {named} is unsupported unless a conversation excerpt below "
        f"shows {named} saying it."
    )
