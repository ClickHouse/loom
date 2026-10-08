"""The latest statement wins — decided from the evidence clocks, not from
the reader's sense of "direct" (per-turn clocks).

A knowledge-update question ("what is my father's position?") has one
current answer. The fact plane already holds it (the extractor superseded
"corporate paralegal" with "corporate attorney" a minute later), and the
raw chunk that reached the reader still carried the older sentence, so the
reader chose it as "the user's direct statement": the fact and the session
summary said attorney, a chunk said paralegal, the answer was paralegal.

This module reads the clock on every mention of every option in the hits —
a chunk turn's `[role @ YYYY-MM-DD HH:MM]` stamp before the mention, a fact
row's valid_at — and when the option the reader chose was last stated
strictly earlier than another option, sets the letter to the later one and
says so. Only user turns count for chunk mentions (an assistant echo is not
a statement). Untouched when fewer than two options are mentioned with
clocks, when the reader's option is the latest, or when clocks tie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from server.memory.domain.ask_option_guard import _clean, parse_options, question_stem

_STAMP_RE = re.compile(r"\[(?P<role>[a-z_]+) @ (?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]")
_LEAD_RE = re.compile(r"^\s*\(?([A-Ha-h])[\)\.:]\s*")
_PROPER_RE = re.compile(r"\b[A-Z][A-Za-z0-9&'\-]{2,}\b")
_NOT_SUBJECTS = frozenset(
    {
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "how",
        "why",
        "does",
        "did",
        "was",
        "were",
        "has",
        "have",
        "had",
        "the",
        "this",
        "that",
        "these",
        "those",
        "are",
        "can",
        "could",
        "will",
        "would",
        "should",
        "please",
        "tell",
        "according",
        "based",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
)


def subject_terms(question: str) -> list[str]:
    """Proper nouns in the question stem ("WanderLit" in "What exactly is
    WanderLit's scale?"), lower-cased. A statement of an option's text counts
    for the question only when it also names one of them: otherwise
    "three hundred people", said of Budget Boost five days later, outranked
    WanderLit's own "two hundred people". A stem with no proper noun ("what is
    the position of my father?") gates nothing — the fact plane names the
    father by his name, and the question does not."""
    stem = question_stem(question)
    out: list[str] = []
    for m in _PROPER_RE.finditer(stem):
        t = m.group(0).rstrip("'s").rstrip("'")
        if t.lower() in _NOT_SUBJECTS or len(t) < 3:
            continue
        if t.lower() not in out:
            out.append(t.lower())
    return out


def _names_subject(text: str, subjects: list[str]) -> bool:
    if not subjects:
        return True
    low = _clean(text)
    return any(s in low for s in subjects)


@dataclass(frozen=True)
class LatestPick:
    text: str
    changed: bool = False
    from_letter: str = ""
    to_letter: str = ""
    clocks: dict[str, str] | None = None


def _chunk_mentions(excerpt: str, needle: str, subjects: list[str] | None = None) -> list[str]:
    """Stamps of the USER turns in a chunk excerpt whose text contains `needle`
    (and names one of `subjects`, when the question has any)."""
    out: list[str] = []
    stamps = list(_STAMP_RE.finditer(excerpt))
    if not stamps:
        return out
    for i, m in enumerate(stamps):
        seg = excerpt[m.end() : stamps[i + 1].start() if i + 1 < len(stamps) else len(excerpt)]
        if (
            m.group("role") == "user"
            and needle in _clean(seg)
            and _names_subject(seg, subjects or [])
        ):
            out.append(m.group("stamp"))
    return out


def latest_clocks(options: dict[str, str], hits: list[Any], question: str = "") -> dict[str, str]:
    """{letter: latest 'YYYY-MM-DD HH:MM' at which that option's text was
    stated}, over chunk user turns and fact rows (valid_at). With a proper
    noun in the stem, only statements naming it count (see subject_terms)."""
    subjects = subject_terms(question)
    out: dict[str, str] = {}
    for letter, opt in options.items():
        needle = _clean(opt)
        if len(needle) < 3:
            continue
        best = ""
        for h in hits:
            key = str(getattr(h, "memory_key", "") or "")
            excerpt = str(getattr(h, "content_excerpt", "") or "")
            if key.startswith("chunk:"):
                for st in _chunk_mentions(excerpt, needle, subjects):
                    best = max(best, st)
            elif key.startswith("summary:"):
                continue  # a digest restates; it carries no clock of its own
            elif needle in _clean(excerpt) and _names_subject(excerpt, subjects):
                va = str(getattr(h, "valid_at", "") or "")[:16].replace("T", " ")
                if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", va):
                    best = max(best, va)
        if best:
            out[letter] = best
    return out


_HEDGE_OPTION_RE = re.compile(r"^(both |neither |unable to|cannot |none of)", re.I)


def is_value_update_question(question: str, options: dict[str, str]) -> bool:
    """True when the options are alternative VALUES of one attribute ("what
    is my father's position?") rather than the entities a comparison names
    ("who is older, Dylan or Savannah?"). A comparison names its candidates
    in the question and usually offers a "Both the same" hedge; the later
    mention of a candidate's NAME says nothing about which one is current.
    Without this check, correct comparative answers move to whichever person
    was mentioned later."""
    stem = question_stem(question).lower()
    if any(_HEDGE_OPTION_RE.match(v.strip()) for v in options.values()):
        return False
    named = sum(1 for v in options.values() if _clean(v) and _clean(v) in stem)
    return named < 2


def prefer_latest_statement(answer: str, question: str, hits: list[Any]) -> LatestPick:
    """Set the leading letter to the option stated latest when the reader
    chose an option stated strictly earlier. Value-update questions only."""
    options = parse_options(question)
    if not options or not answer or not is_value_update_question(question, options):
        return LatestPick(answer)
    first, sep, rest = answer.partition("\n")
    lead = _LEAD_RE.match(first)
    if not lead:
        return LatestPick(answer)
    chosen = lead.group(1).upper()
    clocks = latest_clocks(options, hits, question)
    if chosen not in clocks or len(clocks) < 2:
        return LatestPick(answer, clocks=clocks)
    latest_letter = max(clocks, key=lambda k: clocks[k])
    if clocks[latest_letter] <= clocks[chosen]:
        return LatestPick(answer, clocks=clocks)
    if sum(1 for k, v in clocks.items() if v == clocks[latest_letter]) > 1:
        return LatestPick(answer, clocks=clocks)  # a tie at the top decides nothing
    body = first[lead.end() :].strip()
    # The verdict and its reason lead; the reader's rationale (which argued for
    # the superseded option) follows, so a truncated answer still reads right.
    new_first = (
        f"{latest_letter}) {options[latest_letter]} — the latest statement "
        f"({clocks[latest_letter]}) supersedes {options[chosen]!r} ({clocks[chosen]}). "
        f"Reader: {body}"
    )
    return LatestPick(
        new_first + (sep + rest if sep else ""),
        changed=True,
        from_letter=chosen,
        to_letter=latest_letter,
        clocks=clocks,
    )


__all__ = [
    "LatestPick",
    "is_value_update_question",
    "latest_clocks",
    "prefer_latest_statement",
    "subject_terms",
]
