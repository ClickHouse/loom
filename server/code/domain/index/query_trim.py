"""A long question is asked with its rarest words and its first page.

An issue-shaped question arrives as the issue body: the template's headings ("Company or
project name", "Describe what's wrong", "_No response_") come first, the subject comes
after them, and the reproducer's URL hashes come last. The lexical leg's token list is
the FIRST 32 lexemes in first-seen order (code_search/), so on such a question the
headings and the prose stopwords take the slots, the subject words past position 32
never reach the gate, and the `multiSearchAny` gate admits about half of the corpus
(44–56 % of 267k chunks on the four longest questions of one consumer's week; the rarest
32 of the same words admit 7–33 %).

This module is the plan for such a question, pure and unit-testable:

* `strip_boilerplate` removes the template headings by a fixed list — they
  arrive flattened into one line, so the match is a case-insensitive phrase,
  not a line — and counts what it removed.
* `select_rarest` keeps the K rarest tokens by document frequency from the
  corpus statistics the leg already reads, never dropping a token that is
  the whole form of an identifier (camel / snake / `a::b`), and ranking
  last the tokens that cannot help: hash-like hex fragments, bare numbers,
  and words the corpus does not contain (df 0 admits no row and scores
  nothing).
* the vector leg embeds the first `EMBED_MAX_WORDS` words of the cleaned
  text instead of the raw body.

Nothing here runs for a question whose tokenizer output fits the cap: the
plan's `lexical_query`, `embed_text` and `tokens` are then the raw inputs,
so every statement and the embedding cache key are byte-identical to the
untrimmed path (asserted in tests/unit/test_code_search_query_trim.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from server.code.domain.extract.tokenize import code_tokens, compound_identifiers

# The issue template's headings, as GitHub renders them into a body. The
# match is a case-insensitive phrase anywhere in the text (a consumer that
# pastes the body flattens its lines), optionally followed by a colon.
BOILERPLATE_HEADINGS: tuple[str, ...] = (
    "Company or project name",
    "Describe what's wrong",
    "Describe the unexpected behaviour",
    "Describe the unexpected behavior",
    "Describe the situation",
    "How to reproduce",
    "Expected behavior",
    "Expected behaviour",
    "Error message and/or stacktrace",
    "Additional context",
    "_No response_",
)

# The lexical leg's list is bounded at 32 lexemes; the trim replaces WHICH 32
# (rarest instead of first), never widens.
LEXICAL_TOKEN_CAP = 32
# The vector leg embeds at most this many whitespace words of the cleaned text (~2,000
# characters of prose: the request cap for `query`).
EMBED_MAX_WORDS = 512

_BOILERPLATE_RE = re.compile(
    "|".join(
        r"(?<![A-Za-z0-9])" + re.escape(h).replace("'", "['’]") + r"(?![A-Za-z0-9])\s*:?"
        for h in sorted(BOILERPLATE_HEADINGS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
# A `Namespace::name` reference: both sides are identifiers to keep. Group 1
# is the name, read from a word's first letter so a long word costs one pass.
_QUALIFIED_RE = re.compile(
    r"(?<![A-Za-z0-9_])[0-9]*+([A-Za-z_][A-Za-z0-9_]*+(?:::[A-Za-z_][A-Za-z0-9_]*+)+)"
)
# Tokens that cannot name a subject: a bare number, or a hex fragment of a
# commit / fiddle hash (letters a–f with at least one digit, 4+ chars).
_NOISE_RE = re.compile(r"^(?:[0-9]+|(?=.*[0-9])[0-9a-f]{4,})$")


@dataclass(frozen=True)
class TrimPlan:
    """What the two legs read for one question. `active` False = raw inputs."""

    lexical_query: str
    embed_text: str
    tokens: list[str]
    active: bool = False
    tokens_in: int = 0
    all_tokens: list[str] = field(default_factory=list)
    keep: frozenset[str] = frozenset()
    cap: int = LEXICAL_TOKEN_CAP
    boilerplate_dropped: int = 0


def strip_boilerplate(text: str) -> tuple[str, int]:
    """The text without the template headings, and how many were removed.
    Whitespace runs left by a removal collapse to one space."""
    cleaned, n = _BOILERPLATE_RE.subn(" ", text)
    if n == 0:
        return text, 0
    return " ".join(cleaned.split()), n


def identifier_forms(text: str) -> frozenset[str]:
    """Lowercased whole forms of the identifiers in `text` that a trim must
    keep: camel / snake compounds (tokenize.compound_identifiers) and every
    side of a `a::b` qualified name. A hash fragment splits at its
    letter-digit edges like an identifier but is noise, not one."""
    keep = {t for t in compound_identifiers(text) if not is_noise_token(t)}
    for m in _QUALIFIED_RE.finditer(text):
        for part in m.group(1).split("::"):
            if len(part) >= 2:
                keep.add(part.lower())
    return frozenset(keep)


def is_noise_token(tok: str) -> bool:
    """A bare number or a hash-like hex fragment: rarest by df, yet no subject."""
    return bool(_NOISE_RE.match(tok))


def select_rarest(
    tokens: list[str], df: dict[str, int], k: int, keep: frozenset[str] = frozenset()
) -> list[str]:
    """The `k` tokens the lexical gate keeps, in their first-seen order:
    identifier forms first (always), then prose tokens the corpus knows by
    ascending df (ties by position), then noise and unknown tokens (df 0),
    which fill remaining slots only when nothing better is left."""
    k = max(int(k), 1)
    kept = [t for t in tokens if t in keep][:k]
    room = k - len(kept)
    rest = [t for t in tokens if t not in keep]
    good = [t for t in rest if df.get(t, 0) > 0 and not is_noise_token(t)]
    good.sort(key=lambda t: (df[t], tokens.index(t)))
    chosen = set(kept) | set(good[:room])
    if len(chosen) < k:
        for t in rest:
            if len(chosen) >= k:
                break
            chosen.add(t)
    return [t for t in tokens if t in chosen]


def embed_text_for(cleaned: str, max_words: int = EMBED_MAX_WORDS) -> str:
    """The first `max_words` whitespace words of the cleaned text."""
    words = cleaned.split()
    return cleaned if len(words) <= max_words else " ".join(words[:max_words])


def plan(
    query: str, extra_terms: list[str] | None, max_tokens: int, work: dict[str, int]
) -> TrimPlan:
    """Decide the two legs' inputs. `max_tokens` (the deployment's
    code_search_query_max_tokens) <= 0 turns the trim off;
    otherwise a question whose tokenizer output exceeds it is stripped of
    boilerplate, its vector text is the cleaned first page, and its lexical
    gate will take the rarest `min(max_tokens, LEXICAL_TOKEN_CAP)` tokens
    once the corpus statistics arrive (select_rarest). `work` receives the
    disclosure counters only when the trim is active."""
    lexical_query = query if not extra_terms else query + " " + " ".join(extra_terms)
    raw_tokens = code_tokens(lexical_query)
    cap = int(max_tokens)
    if cap <= 0 or len(raw_tokens) <= cap:
        return TrimPlan(lexical_query, query, raw_tokens[:LEXICAL_TOKEN_CAP])
    cleaned_q, dropped = strip_boilerplate(query)
    cleaned_lex = cleaned_q if not extra_terms else cleaned_q + " " + " ".join(extra_terms)
    all_tokens = code_tokens(cleaned_lex)
    work["query_trimmed_tokens_in"] = len(raw_tokens)
    work["query_trimmed_boilerplate_lines_dropped"] = dropped
    return TrimPlan(
        lexical_query=cleaned_lex,
        embed_text=embed_text_for(cleaned_q),
        tokens=all_tokens[:LEXICAL_TOKEN_CAP],
        active=True,
        tokens_in=len(raw_tokens),
        all_tokens=all_tokens,
        keep=identifier_forms(cleaned_lex),
        cap=min(cap, LEXICAL_TOKEN_CAP),
        boilerplate_dropped=dropped,
    )
