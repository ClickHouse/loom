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
MIN_ROWS = 2
LEAD = 2


def spelling_candidates(query: str, *, max_words: int) -> list[str]:
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


def pick_spellings(tokens: Iterable[str], rows: Mapping[str, int]) -> dict[str, str]:
    """token -> the spelling most rows use, when that is not the lowercase
    spelling and it clearly leads: at least `MIN_ROWS` rows, and `LEAD` times
    the rows that write the word in lowercase. A closer split keeps the word
    as typed. `rows` maps each spelling to the rows that use it."""
    out: dict[str, str] = {}
    for tok in tokens:
        seen = {s: n for s, n in rows.items() if s.lower() == tok}
        if not seen:
            continue
        best, n = max(seen.items(), key=lambda kv: (kv[1], kv[0]))
        if best != tok and n >= MIN_ROWS and n >= LEAD * seen.get(tok, 0):
            out[tok] = best
    return out


def apply_spellings(text: str, spellings: Mapping[str, str]) -> str:
    """`text` with each whole word found in `spellings` replaced by its
    spelling; every other character is kept."""
    if not spellings:
        return text
    return _WORD_RE.sub(lambda m: spellings.get(m.group(0), m.group(0)), text)
