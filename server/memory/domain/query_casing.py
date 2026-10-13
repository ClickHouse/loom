"""The corpus spelling of a query word written all-lowercase. Pure.

A word is a maximal run of `[A-Za-z0-9_]`, the same rule the lookup
(`server.memory.store.query_spellings`) uses to sample and count, so
`datalakecatalog2` and `datalakecatalog_x` are words of their own, never
`datalakecatalog` with a suffix."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

WORD_PATTERN = r"[A-Za-z0-9_]+"
_WORD_RE = re.compile(WORD_PATTERN)
MIN_WORD_LEN = 4
MAX_CANDIDATES = 8


def casing_candidates(query: str, *, max_words: int) -> list[str]:
    """The query's words of `MIN_WORD_LEN`+ lowercase letters and nothing
    else, in order and deduplicated, when the query has at most `max_words`
    words; [] otherwise. A word with a capital, a digit or an underscore is
    taken as typed."""
    words = _WORD_RE.findall(query or "")
    if not words or max_words <= 0 or len(words) > max_words:
        return []
    out: list[str] = []
    for w in words:
        if len(w) >= MIN_WORD_LEN and w.isalpha() and w.islower() and w not in out:
            out.append(w)
    return out[:MAX_CANDIDATES]


def pick_spellings(tokens: Iterable[str], counts: Mapping[str, int]) -> dict[str, str]:
    """token -> its most frequent spelling in `counts` (spelling ->
    occurrences), for the tokens whose most frequent spelling is not the
    lowercase one. Ties go to the lexicographically first spelling."""
    out: dict[str, str] = {}
    for tok in tokens:
        seen = [(n, s) for s, n in counts.items() if s.lower() == tok and n > 0]
        if not seen:
            continue
        _n, best = min(seen, key=lambda p: (-p[0], p[1]))
        if best != tok:
            out[tok] = best
    return out


def apply_spellings(text: str, spellings: Mapping[str, str]) -> str:
    """`text` with each whole word found in `spellings` replaced by its
    spelling; every other character is kept."""
    if not spellings:
        return text
    return _WORD_RE.sub(lambda m: spellings.get(m.group(0), m.group(0)), text)
