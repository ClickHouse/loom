"""A long question is asked with its opening, its identifiers and its first page.

An issue-shaped question arrives as the issue body: the subject is stated first, then
come the description, a reproducer, its output and the links. The lexical leg sums idf
times presence over at most 32 lexemes, so every lexeme the subject's code does not hold
only adds other chunks to the pool, and the rarest prose of a body is the least likely
to be there: it is rare in code because it is not code vocabulary. On 29 public issues
the chunk of the definition to change holds, weighted by idf, 47 % of a question's first
8 lexemes, 31 % of the next 8 and 23 to 32 % of each later run; it holds 22 % of the
identifiers after the first line and 2 % of the later words that fewer than one chunk
in 1,000 holds.

This module is the plan for such a question, pure and unit-testable:

* `strip_boilerplate` removes the template headings by a fixed list — they
  arrive flattened into one line, so the match is a case-insensitive phrase,
  not a line — and counts what it removed.
* `strip_non_terms` removes what names no subject before the tokenizer splits it:
  URLs, bare numbers and hash-like words.
* `select_terms` keeps the opening (the question's first lexemes, half the list at
  most, whatever its line breaks) and the whole forms of the identifiers (camel /
  snake / `a::b`), never the prose after the opening. Past the cap the opening goes
  first, identifiers before words, the rarest by document frequency first; a lexeme
  the corpus does not contain (df 0 admits no row and scores nothing) fills a slot
  only when nothing better is left.
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

# The lexical leg's list is bounded at 32 lexemes; the trim chooses WHICH, never widens.
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
# An address, not a subject: its host, route words, hash and line anchor.
_URL_RE = re.compile(r"https?://\S+")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


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
    lead: frozenset[str] = frozenset()
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


def strip_non_terms(text: str) -> str:
    """The text without what names no subject: URLs, bare numbers and hash-like words.
    A hash goes whole, before the tokenizer splits it at its letter-digit edges into
    letter-only pieces that read as rare words."""
    return _WORD_RE.sub(
        lambda m: " " if is_noise_token(m.group(0).lower()) else m.group(0),
        _URL_RE.sub(" ", text),
    )


def select_terms(
    tokens: list[str],
    df: dict[str, int],
    k: int,
    keep: frozenset[str],
    lead: frozenset[str],
) -> list[str]:
    """At most `k` tokens for the lexical gate, in their first-seen order: those of the
    opening (`lead`) and the identifier forms (`keep`), never the prose after the
    opening. Past `k` the opening goes first, identifiers before words, then ascending
    df (ties by position); noise and unknown tokens (df 0) rank after everything the
    corpus knows."""
    k = max(int(k), 1)
    position = {t: i for i, t in enumerate(tokens)}

    def rank(t: str) -> tuple[bool, bool, bool, int, int]:
        known = df.get(t, 0) > 0 and not is_noise_token(t)
        return (not known, t not in lead, t not in keep, df.get(t, 0), position[t])

    eligible = [t for t in tokens if t in lead or t in keep]
    chosen = set(sorted(eligible, key=rank)[:k])
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
    gate will take at most `min(max_tokens, LEXICAL_TOKEN_CAP)` tokens once the
    corpus statistics arrive (select_terms): its opening, which is the first
    half of that many lexemes, and its identifiers. `extra_terms` are a
    caller's own choice of terms: they are kept like the opening. `work`
    receives the disclosure counters only when the trim is active."""
    lexical_query = query if not extra_terms else query + " " + " ".join(extra_terms)
    raw_tokens = code_tokens(lexical_query)
    cap = int(max_tokens)
    if cap <= 0 or len(raw_tokens) <= cap:
        return TrimPlan(lexical_query, query, raw_tokens[:LEXICAL_TOKEN_CAP])
    cleaned_q, dropped = strip_boilerplate(query)
    extra = " ".join(extra_terms or ())
    cleaned_lex = strip_non_terms(cleaned_q if not extra else cleaned_q + " " + extra)
    all_tokens = code_tokens(cleaned_lex)
    cap = min(cap, LEXICAL_TOKEN_CAP)
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
        lead=frozenset(all_tokens[: max(cap // 2, 1)]) | frozenset(code_tokens(extra)),
        cap=cap,
        boilerplate_dropped=dropped,
    )
