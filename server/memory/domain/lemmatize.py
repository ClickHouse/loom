"""Cheap-regex English suffix-stripper — no heavy NLP deps.

Compresses inflected forms ("deploys/deploying/deployed") into a shared
token so a query for any form retrieves all variants. Over-strips on
double letters and passes irregulars through unchanged; substring/BM25
matching tolerates both.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\b[a-zA-Z][a-zA-Z']{1,30}\b")


def _strip_suffix(token: str) -> str:
    """Strip one inflectional suffix; longest patterns first."""
    t = token.lower()
    if t.endswith("ied") and len(t) > 5:
        return t[:-3] + "y"
    if t.endswith("ied") and len(t) > 4:
        return t[:-1]
    if t.endswith("ed") and len(t) > 4:
        return t[:-2]
    if t.endswith("ing") and len(t) > 5:
        return t[:-3]
    if t.endswith("ier") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("iest") and len(t) > 5:
        return t[:-4] + "y"
    if t.endswith("er") and len(t) > 4:
        return t[:-2]
    if t.endswith("est") and len(t) > 5:
        return t[:-3]
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("'s"):
        return t[:-2]
    if t.endswith("s") and not t.endswith(("ss", "us", "is", "as", "os")) and len(t) > 3:
        return t[:-1]
    return t


def lemmatize(text: str) -> str:
    """Lowercase, tokenize, strip suffixes, rejoin (non-token spans → space)."""
    if not text:
        return ""
    out: list[str] = []
    last_end = 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() > last_end:
            out.append(" ")
        out.append(_strip_suffix(m.group(0)))
        last_end = m.end()
    return "".join(out).strip()


def lemma_tokens(text: str) -> list[str]:
    """Lemmatized token list, deduplicated, order-preserving."""
    seen: set[str] = set()
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(text or ""):
        t = _strip_suffix(m.group(0))
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            tokens.append(t)
    return tokens


# Must match ClickHouse stem(t,'en') byte-for-byte (both Snowball english v2)
# so query tokens hit the server-side content_excerpt_stem index.
try:
    import snowballstemmer  # type: ignore[import-untyped]  # ships no type information

    _SNOWBALL_EN = snowballstemmer.stemmer("english")
except Exception:  # noqa: BLE001 — no stemmer: callers fall open to plain tokens
    _SNOWBALL_EN = None


def snowball_stem_tokens(tokens: list[str]) -> list[str]:
    """Snowball-stem tokens, order-preserving. Fail-open if package missing."""
    if _SNOWBALL_EN is None or not tokens:
        return list(tokens)
    return [_SNOWBALL_EN.stemWord(t) for t in tokens]


# Must match ClickHouse-server-side lemmatize('en', ...) byte-for-byte. lemmagen3 is the
# `[lemma]` extra: the image and `[dev]` install it; a CLI-only host does not, and every
# caller below falls open.
try:
    import lemmagen3

    _LEMMA_EN = lemmagen3.Lemmatizer("en")
except Exception:  # noqa: BLE001 — no [lemma] extra: callers fall open
    _LEMMA_EN = None


def lemmagen_lemma_tokens(tokens: list[str]) -> list[str]:
    """Lemmagen3-lemmatize tokens; catches irregulars Snowball misses
    (went/go, mice/mouse). Order-preserving. Fail-open if package missing."""
    if _LEMMA_EN is None or not tokens:
        return list(tokens)
    return [_LEMMA_EN.lemmatize(t) for t in tokens]


# Alpha runs only — mirrors ClickHouse splitByNonAlpha(lower(...)) closely
# enough that the text(tokenizer='splitByNonAlpha') index re-tokenizes the
# stored string into the same lemma tokens the query side matches against.
_LEMMA_TOKEN_RE = re.compile(r"[A-Za-z]+")


def lemmagen_lemma_text(text: str) -> str:
    """App-side equivalent of the ClickHouse-server expression
    ``arrayStringConcat(arrayMap(t -> lemmatize('en', t),
    splitByNonAlpha(lower(text))), ' ')``.

    Populates loom.memory_embeddings.content_excerpt_lemma at write time so the
    lemma lexical column needs NO server-side <lemmatizers> dictionary. That is
    the path that lets Loom run on ClickHouse Cloud, where the lemmatizer config
    and its en.bin aren't reachable. Uses the same lemmagen3 'en' model the
    query side uses (lemmagen_lemma_tokens), so stored lemmas and query lemmas
    agree. Fail-open (identity join) if lemmagen3 is missing."""
    if not text:
        return ""
    tokens = _LEMMA_TOKEN_RE.findall(text.lower())
    if not tokens:
        return ""
    if _LEMMA_EN is None:
        return " ".join(tokens)
    return " ".join(_LEMMA_EN.lemmatize(t) for t in tokens)
