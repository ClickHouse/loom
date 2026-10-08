"""Lexical answer scores — token F1 and BLEU-1 on a SIMPLIFIED answer — so a
loom number can sit next to published lexical tables.

Published comparisons score every system with token F1 and BLEU-1 after one
uniform simplification step: the generated answer is reduced to its shortest
string that still answers the question, because both metrics punish verbosity,
not wrongness. One item makes the point — gold "7 May 2023":

    "Caroline went to the LGBTQ support group on May 7, 2023."  F1 0.3750  BLEU-1 0.2307
    "May 7, 2023"  (simplified)                                  F1 0.8571  BLEU-1 0.7500
    "8 May 2023"   (wrong)                                       F1 0.6667  BLEU-1 0.6667

The wrong answer outscores the right unsimplified one. Loom's harnesses grade
with a judge for that reason (bench/README.md "Metric comparison hygiene");
these scores are ADDITIONAL columns for comparability,
never a replacement for the judge.

Tokenisation reproduces the published numbers above exactly (the test pins them):
lowercase, words and punctuation marks as separate tokens, articles kept.
`normalize="squad"` gives the SQuAD variant memoryagentbench.py uses (articles
and punctuation dropped) for anyone matching that benchmark's own scorer.
"""

from __future__ import annotations

import math
import re
import string
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_ARTICLES_RE = re.compile(r"\b(a|an|the)\b")


def paper_tokens(text: str) -> list[str]:
    """Lowercased words and punctuation marks, each its own token."""
    return _TOKEN_RE.findall((text or "").lower())


def squad_tokens(text: str) -> list[str]:
    """SQuAD normalisation: lowercase, punctuation and articles removed."""
    s = (text or "").lower()
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = _ARTICLES_RE.sub(" ", s)
    return s.split()


_TOKENIZERS: dict[str, Callable[[str], list[str]]] = {
    "paper": paper_tokens,
    "squad": squad_tokens,
}


def token_f1(prediction: str, gold: str, *, normalize: str = "paper") -> float:
    """Bag-of-tokens F1 between one prediction and one gold answer."""
    tok = _TOKENIZERS[normalize]
    p, g = tok(prediction), tok(gold)
    if not p or not g:
        return 1.0 if p == g else 0.0
    overlap = sum((Counter(p) & Counter(g)).values())
    if overlap == 0:
        return 0.0
    precision, recall = overlap / len(p), overlap / len(g)
    return 2 * precision * recall / (precision + recall)


def bleu1(prediction: str, gold: str, *, normalize: str = "paper") -> float:
    """BLEU with unigrams only: clipped unigram precision times the brevity
    penalty exp(1 - r/c) when the candidate is shorter than the reference."""
    tok = _TOKENIZERS[normalize]
    p, g = tok(prediction), tok(gold)
    if not p or not g:
        return 1.0 if p == g else 0.0
    clipped = sum((Counter(p) & Counter(g)).values())
    if clipped == 0:
        return 0.0
    precision = clipped / len(p)
    bp = 1.0 if len(p) >= len(g) else math.exp(1.0 - len(g) / len(p))
    return bp * precision


def rouge_l(prediction: str, gold: str, *, normalize: str = "paper") -> tuple[float, float]:
    """ROUGE-L (F1, recall) on the longest common token subsequence — two common
    LongMemEval axes, computed with the same tokeniser as `token_f1` so the three
    columns move together. Recall = LCS / |gold|, precision = LCS / |prediction|, F1
    their harmonic mean (β = 1, the rouge-score default)."""
    tok = _TOKENIZERS[normalize]
    p, g = tok(prediction), tok(gold)
    if not p or not g:
        return (1.0, 1.0) if p == g else (0.0, 0.0)
    # O(|p|·|g|) LCS on short answers; a row of |g|+1 ints is all the state.
    prev = [0] * (len(g) + 1)
    for a in p:
        cur = [0]
        for j, b in enumerate(g, start=1):
            cur.append(prev[j - 1] + 1 if a == b else max(prev[j], cur[j - 1]))
        prev = cur
    lcs = prev[-1]
    if lcs == 0:
        return (0.0, 0.0)
    precision, recall = lcs / len(p), lcs / len(g)
    return (2 * precision * recall / (precision + recall), recall)


# --- type-aware temporal scoring --------------------------------------------
# A temporal answer is graded by its TYPE: a timestamp by unit-aware
# equality (any format that names the same day), a duration by ε-exact match
# on the number of days (ε = 1). A judge folds both into "wrong"; these columns
# say whether a miss is a format, an off-by-one from date arithmetic, or a
# different event, which decides between the reader, the server-side dateDiff
# prong and retrieval.

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for m in names
}
_ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DMY_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MDY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_MY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?,?\s*(\d{4})\b")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_NUM_WORDS = {
    "a": 1,
    "an": 1,
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
    "couple": 2,
    "few": 3,
    "several": 3,
}
_DURATION_RE = re.compile(
    r"\b(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|couple(?: of)?|few|several)\s+(day|week|month|year|hour|minute)s?\b",
    re.IGNORECASE,
)
_UNIT_DAYS = {
    "minute": 1 / 1440,
    "hour": 1 / 24,
    "day": 1.0,
    "week": 7.0,
    "month": 30.0,
    "year": 365.0,
}


def parse_date(text: str) -> tuple[int, int | None, int | None] | None:
    """(year, month, day) named by a date string in any common format;
    month / day None when the text names only a month or a year. None when
    no date is present. The first date in the text wins."""
    t = (text or "").strip()
    m = _ISO_RE.search(t)
    if m:
        return (int(m[1]), int(m[2]), int(m[3]))
    m = _DMY_RE.search(t)
    if m and m[2].lower() in _MONTHS:
        return (int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
    m = _MDY_RE.search(t)
    if m and m[1].lower() in _MONTHS:
        return (int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    m = _MY_RE.search(t)
    if m and m[1].lower() in _MONTHS:
        return (int(m[2]), _MONTHS[m[1].lower()], None)
    m = _YEAR_RE.search(t)
    if m:
        return (int(m[1]), None, None)
    return None


_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_RELATIVE_GOLD_RE = re.compile(
    r"\b(?:the\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|day|weekend|month)\s+"
    r"(before|after|prior to|following)\b",
    re.IGNORECASE,
)


def gold_date_window(gold: str) -> tuple[date, date] | None:
    """The calendar window a gold answer names. A plain date is a one-day window; a
    LoCoMo-style relative gold ("the Sunday before 25 May 2023", "the week before 9 June
    2023") is resolved against the date it anchors on — the anchor itself is NOT the
    answer, it is the session date the reader is tempted to echo. None when the gold
    names no date, or names only a month or a year (see date_match)."""
    d = parse_date(gold)
    if d is None or d[1] is None or d[2] is None:
        return None
    anchor = date(d[0], d[1], d[2])
    m = _RELATIVE_GOLD_RE.search(gold or "")
    if not m:
        return (anchor, anchor)
    unit, direction = m[1].lower(), m[2].lower()
    back = direction in ("before", "prior to")
    if unit in _WEEKDAYS:
        wd = _WEEKDAYS[unit]
        if back:
            delta = (anchor.weekday() - wd - 1) % 7 + 1
            day = anchor - timedelta(days=delta)
        else:
            delta = (wd - anchor.weekday() - 1) % 7 + 1
            day = anchor + timedelta(days=delta)
        return (day, day)
    span = {"day": 1, "week": 7, "weekend": 3, "month": 30}[unit]
    if back:
        return (anchor - timedelta(days=span), anchor - timedelta(days=1))
    return (anchor + timedelta(days=1), anchor + timedelta(days=span))


def date_match(prediction: str, gold: str) -> bool | None:
    """Unit-aware timestamp equality: True when both name the same date at
    the gold's precision (a gold month matches any day in it; a gold year any
    date in it). None when the gold names no date — the column does not
    apply. "September 24, 2025" == "2025-09-24" == "24 Sep 2025"."""
    g = parse_date(gold)
    if g is None:
        return None
    p = parse_date(prediction)
    if p is None:
        return False
    gy, gm, gd = g
    py, pm, pd = p
    if gm is None:  # gold names a year
        return gy == py
    if gd is None:  # gold names a month
        return gy == py and gm == pm
    window = gold_date_window(gold)
    if window is None:
        return False
    lo, hi = window
    if pm is not None and pd is None:
        # A month-only answer is right at its precision when the gold window
        # lies inside that month ("late June 2023" for the week before 27 June).
        return lo.year == py and lo.month == pm and hi.year == py and hi.month == pm
    if pm is None or pd is None:
        return False
    # The prediction may itself be relative ("the week before 2023-06-02"):
    # resolve it the same way and ask whether the two windows overlap.
    pwin = gold_date_window(prediction)
    if pwin is None:
        return False
    plo, phi = pwin
    return plo <= hi and lo <= phi


def duration_days(text: str) -> float | None:
    """The first duration in the text, in days ("two weeks" → 14, "3 months"
    → 90, "a few years" → 1095). None when no duration is present."""
    m = _DURATION_RE.search(text or "")
    if not m:
        return None
    n_raw = m[1].lower().replace(" of", "")
    n = float(n_raw) if n_raw.replace(".", "", 1).isdigit() else float(_NUM_WORDS.get(n_raw, 0))
    return n * _UNIT_DAYS[m[2].lower()]


def duration_match(prediction: str, gold: str, *, eps_days: float = 1.0) -> bool | None:
    """ε-exact match on durations: True when the predicted length is within
    `eps_days` of the gold's (ε = 1 day by default; a gold in months or
    years widens ε to 4% of its own length so "3 months" accepts "13 weeks").
    None when the gold names no duration."""
    g = duration_days(gold)
    if g is None:
        return None
    p = duration_days(prediction)
    if p is None:
        return False
    return abs(p - g) <= max(eps_days, 0.04 * g)


def temporal_kind(gold: str) -> str:
    """Which type-aware column applies to a gold answer: "date" (names a
    day or month), "year", "duration", or "other" (an event, a place)."""
    g = (gold or "").strip()
    if (
        duration_days(g) is not None
        and not _ISO_RE.search(g)
        and not _DMY_RE.search(g)
        and not _MDY_RE.search(g)
    ):
        return "duration"
    d = parse_date(g)
    if d is None:
        return "other"
    return "year" if d[1] is None and _YEAR_RE.fullmatch(g) else "date"


# --- simplification -------------------------------------------------------

# The published simplification prompt, reproduced so the step is the same one.
SIMPLIFY_PROMPT = (
    "Your task is to act as an answer simplifier. I will give you a question and a "
    "full-sentence answer. You must reduce the answer to its most critical component.\n"
    "Follow these rules:\n"
    "1. **Extract the Core Information:** Identify the primary piece of information "
    "that directly answers the question.\n"
    '2. **Remove Extraneous Phrases:** Eliminate phrases like "Based on the information '
    'provided...", "The answer is...", "As per the document...", etc.\n'
    "3. **Omit Explanations:** Do not include any justifications, reasoning, or "
    "additional context from the original answer.\n"
    "4. **Be Concise:** The output should be the shortest possible string that still "
    "accurately answers the question.\n"
    "Example:\n"
    '- Question: "What degree did I graduate with?"\n'
    '- Original Answer: "Based on the information provided, you graduated with a degree '
    'in Business Administration."\n'
    '- Simplified Answer: "Business Administration"\n'
    "Here is the question and answer to simplify:\n"
    "- Question: {question}\n"
    "- Original Answer: {answer}\n"
    "- Simplified Answer:"
)


def build_simplify_prompt(question: str, answer: str) -> str:
    return SIMPLIFY_PROMPT.format(question=question.strip(), answer=answer.strip())


def parse_simplified(reply: str, *, fallback: str) -> str:
    """The model's simplified answer: first non-empty line, label and quotes
    stripped. An empty reply keeps the original answer (a blank scores 0 and
    would read as a wrong answer, not a failed call)."""
    for line in (reply or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^\W*simplified answer\W*", "", s, flags=re.IGNORECASE).strip()
        s = s.strip().strip('"').strip("'").strip()
        if s:
            return s
    return fallback


_LEAD_IN_RE = re.compile(
    r"^\s*(?:based on (?:the|your|our)?\s*(?:information|memories|context|records|conversation)"
    r"(?: provided| available| stored)?|according to (?:the|your)?\s*\w+|as per (?:the|your)?\s*\w+"
    r"|the answer is|it (?:seems|appears) that|from (?:the|your) (?:memories|conversation|notes))"
    r"\s*[,:]?\s*",
    re.IGNORECASE,
)


def simplify_heuristic(answer: str) -> str:
    """Model-free stand-in for the simplification step: drop a lead-in phrase,
    keep the first sentence, drop the final period. Weaker than the model
    (it cannot extract "Business Administration" from a clause) but makes the
    columns computable without a second model; the CSV says which was used."""
    s = (answer or "").strip()
    if not s:
        return s
    s = _LEAD_IN_RE.sub("", s)
    # First sentence: a terminator followed by whitespace and a capital, or
    # the end. A period inside a number ("7.5") has no whitespace after it.
    m = re.search(r"[.!?](?=\s+[A-Z])|[.!?]\s*$", s)
    if m:
        s = s[: m.start()]
    return s.strip().rstrip(".").strip()


def label_agreement(a: list[str], b: list[str]) -> float:
    """Share of positions where two label sequences agree (0.0-1.0)."""
    if not a or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa between two raters' labels; None when undefined (empty,
    unequal lengths, or both raters constant so chance agreement is 1).

    A judge is validated against a second judge on a sample before its number
    is quoted; a loom bench that prints an accuracy prints this next to it so the reader knows how
    much of the noise band is the judge's."""
    if not a or len(a) != len(b):
        return None
    n = len(a)
    labels = sorted(set(a) | set(b))
    po = label_agreement(a, b)
    pe = sum((a.count(lb) / n) * (b.count(lb) / n) for lb in labels)
    if pe >= 1.0:
        return None
    return round((po - pe) / (1.0 - pe), 4)
