"""A comparison is decided by the two values the answer states, not by the
reader's sentence about them.

Comparative multiple-choice items ask "who is older, X or Y", "which event lasts
longer", "whose birthday comes later in the year", and offer the two
candidates plus a hedge ("Both the same"). The reader retrieves both values
and then writes the wrong name a fifth of the time: "Dylan Carter is 40;
Savannah Reed is 31, so Savannah" (older = larger), "December 20th is later
than April 17th, so Silas" (the April one), "both are 28, her March
birthday makes her older" (equal ages are a tie). The values are right and
the comparison is wrong — the same shape as the count verifiers.

This module reads the comparative from the question (older / younger,
taller / shorter, longer / shorter duration, larger / smaller, later /
earlier birthday, higher / lower), finds each candidate's value in the
answer text — the number, duration or month-day nearest the candidate's
name — and picks the option the comparison yields: the winning candidate,
or the "Both the same" option when the values are equal. It never runs
without both values, and never overrides an answer it agrees with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from server.memory.domain.ask_option_guard import _clean, parse_options, question_stem
from server.memory.domain.ask_threshold import parse_quantity

_LEAD_RE = re.compile(r"^\s*\(?([A-Ha-h])[\)\.:]\s*")
_HEDGE_SAME_RE = re.compile(r"^(both the same|the same|same|equal)", re.I)
_HEDGE_OTHER_RE = re.compile(r"^(both incorrect|neither|unable to|cannot |none of)", re.I)

# comparative word → (kind, larger_wins)
_COMPARATIVES: dict[str, tuple[str, bool]] = {
    "older": ("number", True),
    "younger": ("number", False),
    "taller": ("number", True),
    "shorter": ("number", False),
    "heavier": ("number", True),
    "lighter": ("number", False),
    "larger": ("number", True),
    "bigger": ("number", True),
    "smaller": ("number", False),
    "higher": ("number", True),
    "lower": ("number", False),
    "more": ("number", True),
    "fewer": ("number", False),
    "less": ("number", False),
    "longer": ("duration", True),
    "later": ("date", True),
    "earlier": ("date", False),
    "sooner": ("date", False),
}
_COMP_RE = re.compile(
    r"\b(older|younger|taller|shorter|heavier|lighter|larger|bigger|smaller|higher|lower|"
    r"more|fewer|less|longer|later|earlier|sooner)\b",
    re.I,
)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
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
        ]
    )
}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_DATE_WORD_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?\b"
    r"|\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    r"|\b\d{4}-(\d{2})-(\d{2})\b",
    re.I,
)
_NUM_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


@dataclass(frozen=True)
class Compared:
    text: str
    changed: bool = False
    letter: str = ""
    values: dict[str, float] | None = None
    comparative: str = ""


def comparative_of(question: str) -> tuple[str, str, bool] | None:
    """(word, kind, larger_wins) for the first comparative the stem names."""
    stem = question_stem(question)
    m = _COMP_RE.search(stem)
    if not m:
        return None
    w = m.group(1).lower()
    kind, larger = _COMPARATIVES[w]
    if kind == "duration" and not re.search(r"duration|last|long", stem, re.I):
        kind = "number"
    return w, kind, larger


def _month_day(text: str) -> float | None:
    m = _DATE_WORD_RE.search(text)
    if not m:
        return None
    if m.group(1):
        mo, d = _MONTHS.get(m.group(1).lower()[:3]), int(m.group(2))
    elif m.group(3):
        mo, d = _MONTHS.get(m.group(4).lower()[:3]), int(m.group(3))
    else:
        mo, d = int(m.group(5)), int(m.group(6))
    return None if mo is None else mo * 100 + d


def _positions(low: str, key: str) -> list[int]:
    """Every mention of the full name, then every mention of the first name."""
    out: list[int] = []
    first = key.split(" ")[0]
    for needle in (key, first if len(first) >= 3 and first != key else ""):
        if not needle:
            continue
        start = 0
        while True:
            i = low.find(needle, start)
            if i < 0:
                break
            out.append(i)
            start = i + len(needle)
    return out


def _value_in(seg: str, kind: str) -> float | None:
    if kind == "date":
        return _month_day(seg)
    if kind == "duration":
        if re.search(r"day|week|month|year|hour", seg):
            return parse_quantity(seg, temporal=True)
        return None
    nums = [float(x) for x in _NUM_TOKEN_RE.findall(re.sub(r"\d{4}-\d{2}-\d{2}", "", seg))]
    return nums[0] if nums else None


def value_near(answer: str, name: str, kind: str, others: tuple[str, ...] = ()) -> float | None:
    """The value stated closest after (else before) a mention of the
    candidate's name in the answer: a month-day for dates, days for
    durations, a number otherwise. Every mention is tried, first hit wins.
    In the letter-first head ("A) Asher Reid — birthday 2025-01-09 …") the
    clause after the dash belongs to the name unless another candidate is
    named in it first."""
    # A line break is a clause boundary: the reader may put the letter and
    # the name on one line and the two measurements on the next ("C) Maya
    # Sinclair\nSienna Hale is 154 cm tall, while Maya Sinclair is 175") —
    # collapsed to one line, Maya's "nearest value after" was Sienna's 154.
    low = _clean((answer or "").replace("\n", " ; "))
    key = _clean(name)
    other_keys = [_clean(o) for o in others if _clean(o) and _clean(o) != key]
    for pos in _positions(low, key):
        tail = re.split(r";|\.\s|\[|—", low[pos:], maxsplit=1)[0]
        # The value after the name must come before another candidate is
        # named: past that point the numbers are theirs.
        cut = min((tail.find(o) for o in other_keys if o in tail[len(key) :]), default=-1)
        if cut > 0:
            tail = tail[:cut]
        head = low[max(0, pos - 60) : pos]
        for seg in (tail, head):
            v = _value_in(seg, kind)
            if v is not None:
                return v
        after = low[pos + len(tail) :]
        if after.startswith("—") or after.startswith(" —"):
            clause = re.split(r";|\.\s|\[", after.lstrip(" —"), maxsplit=1)[0]
            first_other = min((clause.find(o) for o in other_keys if o in clause), default=-1)
            v = _value_in(clause if first_other < 0 else clause[:first_other], kind)
            if v is not None:
                return v
    return None


_BOTH_RE = re.compile(
    r"\bboth (?:are|were|at|aged|last|lasted|measure|stand at)\s+(\d+(?:\.\d+)?)", re.I
)


def verify_comparison(answer: str, question: str) -> Compared:
    comp = comparative_of(question)
    options = parse_options(question)
    if not comp or not options or not answer:
        return Compared(answer)
    word, kind, larger_wins = comp
    cands = {
        k: v
        for k, v in options.items()
        if not _HEDGE_SAME_RE.match(v) and not _HEDGE_OTHER_RE.match(v)
    }
    same = next((k for k, v in options.items() if _HEDGE_SAME_RE.match(v)), "")
    if len(cands) != 2:
        return Compared(answer)
    first = answer.split("\n", 1)[0]
    lead = _LEAD_RE.match(first)
    chosen = lead.group(1).upper() if lead else ""
    values = {
        k: value_near(answer, v, kind, tuple(o for kk, o in cands.items() if kk != k))
        for k, v in cands.items()
    }
    both = _BOTH_RE.search(answer or "")
    if both and kind != "date":
        values = {k: float(both.group(1)) for k in cands}  # "both are 28": a stated tie
    if any(v is None for v in values.values()):
        return Compared(answer, values=None, comparative=word)
    vals: dict[str, float] = {k: float(v) for k, v in values.items() if v is not None}
    (ka, va), (kb, vb) = list(vals.items())
    if va == vb:
        letter = same
    else:
        letter = ka if (va > vb) == larger_wins else kb
    if not letter or letter == chosen:
        return Compared(answer, letter=letter or chosen, values=vals, comparative=word)
    body = first[lead.end() :].strip() if lead else first.strip()
    shown = ", ".join(f"{options[k]}={_fmt(v, kind)}" for k, v in vals.items())
    note = f"(computed: {shown}; '{word}' → {options[letter]})"
    new_first = f"{letter}) {options[letter]} — {body} {note}"
    rest = answer.split("\n", 1)
    return Compared(
        new_first + ("\n" + rest[1] if len(rest) > 1 else ""),
        changed=True,
        letter=letter,
        values=vals,
        comparative=word,
    )


def _fmt(v: float, kind: str) -> str:
    if kind == "date":
        return f"{int(v) // 100:02d}-{int(v) % 100:02d}"
    if kind == "duration":
        return f"{v:g} days"
    return f"{v:g}"


__all__ = ["Compared", "comparative_of", "value_near", "verify_comparison"]
