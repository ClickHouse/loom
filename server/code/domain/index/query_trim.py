"""A long question is asked with its identifiers, the words it repeats and its first page.

An issue-shaped question arrives as the issue body: a greeting or a template, the
subject, a reproducer, its output and the links, in no fixed order. The lexical leg
sums idf times presence over at most `LEXICAL_TOKEN_CAP` lexemes, so every lexeme the
subject's code does not hold only adds other chunks to the pool. What marks a subject
lexeme is not where it stands or how rare it is: a word that is rare in code is rare
because it is not code vocabulary, and an opening can be a greeting. A question names
its subject as code, or returns to it.

This module is the plan for such a question, pure and unit-testable:

* `strip_boilerplate` removes the template headings by a fixed list — they
  arrive flattened into one line, so the match is a case-insensitive phrase,
  not a line — and counts what it removed.
* `strip_non_terms` removes what names no subject before the tokenizer splits it:
  URLs and hashes.
* `select_terms` keeps, for at most `WORD_SHARE` of the list, the words: those the
  question says more than once first, then the others, each in reading order. The
  whole forms of the identifiers (camel / snake / `a::b`) take the rest, then the
  other lexemes of the terms a caller added, the rarest when there are more than
  fit. An English function word is not a word to keep. A word that more than
  `COMMON_SHARE` of the chunks hold, a bare number, and a lexeme the corpus does not
  contain (df 0 admits no row and scores nothing) fill a slot only when nothing
  better is left.
* the vector leg embeds the first `EMBED_MAX_WORDS` words of the cleaned
  text instead of the raw body.

Known limits: a subject the question states once, as a plain word, after its word
share is already taken is not asked for, nor is one that more than `COMMON_SHARE` of
the chunks hold when other words fill the share; the function words are English.

Nothing here runs for a question whose tokenizer output fits the cap: the
plan's `lexical_query`, `embed_text` and `tokens` are then the raw inputs,
so every statement and the embedding cache key are byte-identical to the
untrimmed path (asserted in tests/unit/test_code_search_query_trim.py).
"""

from __future__ import annotations

import re
from collections import Counter
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
# Words take at most this share of the list, also when the identifiers leave slots
# free: a word said once is as often prose as subject, and each one adds the chunks
# that hold it to the pool. The identifiers take the rest; more than fit are cut to
# the rarest.
WORD_SHARE = 0.5
# A word that more of the chunks hold than this ranks little and opens the gate to
# that share of the namespace: it is asked for after every other word.
COMMON_SHARE = 1 / 8
# The vector leg embeds at most this many whitespace words of the cleaned text (~2,000
# characters of prose: the request cap for `query`).
EMBED_MAX_WORDS = 512
# English function words: a question repeats them more than its subject, and they name
# none. A closed class, so a fixed list.
_FUNCTION_WORDS = frozenset(
    "a an the is are was were be been being am has have had do does did can cannot could "
    "should would will may might must shall of to in on at for with by from as into about "
    "and or but if then than so because that this these those it its you your we our they "
    "their them he she his her my me which who what when where why how not no there here".split()
)

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
# A commit or fiddle hash: seven or more hex characters, the shortest form git prints.
_HEX_RE = re.compile(r"[0-9a-f]{7,}")
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
    said: dict[str, int] = field(default_factory=dict)
    own: frozenset[str] = frozenset()
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
    side of a `a::b` qualified name. A hash splits at its letter-digit edges
    like an identifier but is not one."""
    keep = {t for t in compound_identifiers(text) if not is_hash(t)}
    for m in _QUALIFIED_RE.finditer(text):
        for part in m.group(1).split("::"):
            if len(part) >= 2:
                keep.add(part.lower())
    return frozenset(keep)


def is_hash(word: str) -> bool:
    """A lowercased word of seven or more hex characters, letters among two or more
    digits: a commit or fiddle hash. A short code (`f401`, `e164`) and a word spelled
    in a to f around one digit (`face2face`) are names; a number stays a number."""
    return bool(_HEX_RE.fullmatch(word)) and 2 <= sum(c.isdigit() for c in word) < len(word)


def strip_non_terms(text: str) -> str:
    """The text without what names no subject: URLs and hashes. A hash goes whole,
    before the tokenizer splits it at its letter-digit edges into letter-only pieces
    that read as rare words."""
    return _WORD_RE.sub(
        lambda m: " " if is_hash(m.group(0).lower()) else m.group(0),
        _URL_RE.sub(" ", text),
    )


def term_counts(text: str) -> dict[str, int]:
    """How many words of `text` hold each lexeme, whole or as a subtoken."""
    counts: Counter[str] = Counter()
    for m in _WORD_RE.finditer(text):
        counts.update(code_tokens(m.group(0)))
    return dict(counts)


def select_terms(
    tokens: list[str],
    df: dict[str, int],
    n_chunks: int,
    k: int,
    *,
    keep: frozenset[str],
    said: dict[str, int],
    own: frozenset[str] = frozenset(),
) -> list[str]:
    """At most `k` tokens for the lexical gate, in their first-seen order: the
    identifier forms (`keep`), the caller's own terms (`own`) and, for `WORD_SHARE` of
    `k`, the words: those `said` more than once first, then the others, each in reading
    order; never a function word. Past `k` the words go first, then the identifiers,
    then the other lexemes of the caller's terms, the rarest by document frequency
    first. A word more than `COMMON_SHARE` of `n_chunks` chunks hold ranks after the
    other words; a bare number and an unknown token (df 0) rank after everything the
    corpus knows."""
    k = max(int(k), 1)
    position = {t: i for i, t in enumerate(tokens)}

    def unknown(t: str) -> bool:
        return df.get(t, 0) <= 0 or t.isdigit()

    def common(t: str) -> bool:
        return df.get(t, 0) > n_chunks * COMMON_SHARE

    named = keep | own
    words = [t for t in tokens if t not in named and t not in _FUNCTION_WORDS]
    words.sort(key=lambda t: (unknown(t), common(t), said.get(t, 0) < 2, position[t]))
    kept_words = set(words[: max(int(k * WORD_SHARE), 1)])

    def rank(t: str) -> tuple[bool, bool, bool, int, int]:
        return (unknown(t), t not in kept_words, t not in keep, df.get(t, 0), position[t])

    eligible = [t for t in tokens if t in kept_words or t in named]
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
    corpus statistics arrive (select_terms): its identifiers and its words, those
    it repeats first. `extra_terms` are a caller's own choice of terms, not words of
    the question: none is stripped or filtered, and what is not an identifier's whole
    form among them is asked for after the identifiers. `work` receives the disclosure
    counters only when the trim is active."""
    lexical_query = query if not extra_terms else query + " " + " ".join(extra_terms)
    raw_tokens = code_tokens(lexical_query)
    cap = int(max_tokens)
    if cap <= 0 or len(raw_tokens) <= cap:
        return TrimPlan(lexical_query, query, raw_tokens[:LEXICAL_TOKEN_CAP])
    cleaned_q, dropped = strip_boilerplate(query)
    extra = " ".join(extra_terms or ())
    stripped = strip_non_terms(cleaned_q)
    cleaned_lex = stripped if not extra else stripped + " " + extra
    all_tokens = code_tokens(cleaned_lex)
    if not all_tokens:  # only addresses and hashes: they are all the question says
        cleaned_lex, all_tokens = cleaned_q, code_tokens(cleaned_q)
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
        said=term_counts(cleaned_lex),
        own=frozenset(code_tokens(extra)),
        cap=min(cap, LEXICAL_TOKEN_CAP),
        boilerplate_dropped=dropped,
    )
