"""Recount a COUNT answer from the model's own item list.

Count questions are enumerate-then-count: the synthesizer lists one line per candidate
with a verdict ("— qualifies", "— fails: …", "— unclear: …") and then states the count
on an ANSWER line. The model is reliable at listing and unreliable at counting: on the
hard counting questions it listed five antiques and answered four, listed four siblings
and answered twelve, and aggregative multiple-choice questions sit at 0.60–0.70 with evidence
recall 1.0. The list is right; the number is wrong.

This module counts the verdict lines deterministically and, when the ANSWER's
leading integer disagrees with the number of "— qualifies" lines, rewrites
the integer to the counted one (and the "(N+M if the unclear ones count)"
form when unclear lines exist). It touches nothing else in the answer, never
runs when the answer has two parts ("3 events; 5 people") or no integer, and
reports whether it changed anything so the rate is a column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ANSWER_RE = re.compile(r"(?im)^\s*ANSWER\s*:\s*(.*)$")
_ITEMS_RE = re.compile(r"(?im)^\s*ITEMS\s*:\s*$")
_UNKNOWN_RE = re.compile(r"(?im)^\s*UNKNOWN\s*:\s*$")
_QUALIFIES_RE = re.compile(r"—\s*qualifies\b", re.I)
_UNCLEAR_RE = re.compile(r"—\s*unclear\b", re.I)
_FAILS_RE = re.compile(r"—\s*fails\b", re.I)
# A list of stated quantities ("7 shirts — stated: 7"): the ANSWER is their
# number or sum, not a count of lines, so it is never recounted.
_STATED_RE = re.compile(r"—\s*stated\s*:", re.I)
_LEADING_INT_RE = re.compile(r"^\s*(\d{1,4})\b")
_PAREN_FORM_RE = re.compile(r"^\s*(\d{1,4})\s*\(\s*(\d{1,4})\s+if\b[^)]*\)")
_WORDS = {
    "zero": 0,
    "none": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_LEADING_WORD_RE = re.compile(r"^\s*(" + "|".join(_WORDS) + r")\b", re.I)


@dataclass(frozen=True)
class Recount:
    text: str
    recomputed: bool = False
    qualifies: int = 0
    unclear: int = 0
    stated: int | None = None


def _verdict_counts(items_block: str) -> tuple[int, int, int]:
    """(qualifies, unclear, fails) over the ITEMS lines; a line with no
    verdict marker is not counted either way."""

    q = u = f = 0
    for line in items_block.splitlines():
        s = line.strip()
        if not s or s.startswith(("ITEMS", "UNKNOWN")):
            continue
        if _QUALIFIES_RE.search(s):
            q += 1
        elif _UNCLEAR_RE.search(s):
            u += 1
        elif _FAILS_RE.search(s):
            f += 1
    return q, u, f


def recount(text: str) -> Recount:
    """Rewrite the ANSWER's count from the ITEMS verdicts when they disagree.

    Leaves the text untouched when: there is no ITEMS or ANSWER line; the
    ANSWER has two parts (';'); no item carries a verdict (the model did not
    follow the format, so the list cannot be trusted either); any item is a
    stated quantity ('— stated: N', whose ANSWER is a number or a sum, not a
    line count); or the stated number already equals the count.
    """

    if not text:
        return Recount(text or "")
    items_m = _ITEMS_RE.search(text)
    answers = list(_ANSWER_RE.finditer(text))
    if not items_m or not answers:
        return Recount(text)
    ans_m = answers[-1]
    answer_line = ans_m.group(1)
    if ";" in answer_line.split("[")[0]:
        return Recount(text)  # two-part count; sub-heading parsing is not attempted
    # Items live between ITEMS: and the ANSWER line (UNKNOWN: lines excluded).
    items_block = text[items_m.end() : ans_m.start()]
    unk = _UNKNOWN_RE.search(items_block)
    if unk:
        items_block = items_block[: unk.start()]
    if _STATED_RE.search(items_block):
        return Recount(text)  # quantities, not units: nothing to recount
    q, u, f = _verdict_counts(items_block)
    if q + u + f == 0:
        return Recount(text)
    stated: int | None
    m_int = _LEADING_INT_RE.match(answer_line)
    m_word = None if m_int else _LEADING_WORD_RE.match(answer_line)
    if m_int:
        stated = int(m_int.group(1))
    elif m_word:
        stated = _WORDS[m_word.group(1).lower()]
    else:
        return Recount(text, qualifies=q, unclear=u)
    paren = _PAREN_FORM_RE.match(answer_line)
    stated_with_unclear = int(paren.group(2)) if paren else None
    if stated == q and (u == 0 or stated_with_unclear in (None, q + u)):
        return Recount(text, qualifies=q, unclear=u, stated=stated)
    # Rebuild the leading number (and the unclear form) and keep the rest.
    head_m = paren or m_int or m_word
    assert head_m is not None  # one of the three matched to get here
    rest = answer_line[head_m.end() :]
    if u:
        new_head = f"{q} ({q + u} if the unclear ones count)"
    else:
        new_head = str(q)
    note = " (recounted from the listed items)"
    new_line = f"{new_head}{rest}{note}"
    new_text = text[: ans_m.start(1)] + new_line + text[ans_m.end(1) :]
    return Recount(new_text, recomputed=True, qualifies=q, unclear=u, stated=stated)


__all__ = ["Recount", "recount"]
