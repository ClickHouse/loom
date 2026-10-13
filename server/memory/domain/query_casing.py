"""The corpus spelling of a lowercased query word. An embedder reads
`datalakecatalog` and `DataLakeCatalog` as different strings, so a query typed
in lowercase misses on the vector leg the rows that spell the identifier in
CamelCase, though the lexical arms match case-insensitively. Pure."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_WORD_RE = re.compile(r"[A-Za-z]+")
MIN_WORD_LEN = 4
MAX_CANDIDATES = 8


def casing_candidates(query: str, *, max_words: int) -> list[str]:
    """The query's all-lowercase words of `MIN_WORD_LEN`+ letters, in order and
    deduplicated, when the query has at most `max_words` words (a short,
    keyword-shaped query; a long question carries its own context). A word the
    caller typed with any capital is the caller's spelling and stays."""
    words = _WORD_RE.findall(query or "")
    if not words or max_words <= 0 or len(words) > max_words:
        return []
    out: list[str] = []
    for w in words:
        if len(w) >= MIN_WORD_LEN and w.islower() and w not in out:
            out.append(w)
    return out[:MAX_CANDIDATES]


def pick_spellings(tokens: Iterable[str], counts: Mapping[str, int]) -> dict[str, str]:
    """token -> the corpus's most frequent spelling of it, for the tokens whose
    most frequent spelling is not the lowercase one. `counts` maps each
    spelling seen in the corpus to its occurrences; ties go to the
    lexicographically first spelling so the choice is stable."""
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
    """`text` with each whole lowercase word in `spellings` replaced by its
    spelling; every other character is kept."""
    if not spellings:
        return text
    return _WORD_RE.sub(lambda m: spellings.get(m.group(0), m.group(0)), text)
