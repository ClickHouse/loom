"""Query-text canonicalization for memory search.

Generic-entity taxonomy, irregular-verb lemma map + Snowball stemmer, and the
verb/entity canonicalizers. The canonicalize_* functions normalize both write
and read sides to the same form. Pure functions + data, no I/O.
"""

from __future__ import annotations

from typing import Any

from server.memory.domain.graph_quality import fold_case

_GENERIC_HEADS = frozenset(
    {
        "thing",
        "things",
        "stuff",
        "kind",
        "kinds",
        "type",
        "types",
        "one",
        "ones",
        "way",
        "ways",
        "side",
        "place",
        "places",
        "area",
        "areas",
        "part",
        "parts",
        "piece",
        "pieces",
        "matter",
        "matters",
        "item",
        "items",
        "issue",
        "issues",
        "topic",
        "topics",
        "subject",
        "subjects",
        "case",
        "cases",
        "instance",
        "instances",
        "example",
        "examples",
        "fact",
        "facts",
        "point",
        "points",
    }
)


_NON_SPECIFIC_ADJ = frozenset(
    {
        "good",
        "great",
        "best",
        "nice",
        "recent",
        "new",
        "old",
        "fresh",
        "interesting",
        "useful",
        "helpful",
        "healthy",
        "easy",
        "simple",
        "quick",
        "fast",
        "cheap",
        "free",
        "local",
        "small",
        "big",
        "large",
        "popular",
        "common",
        "specific",
        "general",
        "different",
        "various",
        "appropriate",
        "suitable",
        "relevant",
        "potential",
        "possible",
        "many",
        "few",
        "several",
        "some",
        "any",
        "another",
        "other",
        "main",
        "primary",
        "basic",
        "core",
        "key",
        "important",
        "major",
        "minor",
        "broad",
        "narrow",
        "wide",
        "deep",
        "shallow",
        "high",
        "low",
        "long",
        "short",
        "early",
        "late",
        "current",
        "previous",
        "next",
        "last",
        "first",
        "second",
        "third",
    }
)


_CIRCUMSTANTIAL_MODS = frozenset(
    {
        "solo",
        "alone",
        "team",
        "group",
        "together",
        "individually",
        "manually",
        "automatically",
        "quickly",
        "slowly",
        "carefully",
        "casually",
    }
)


_GENERIC_ENDINGS = frozenset(
    {
        "work",
        "works",
        "job",
        "jobs",
        "stuff",
        "info",
        "data",
        "kind",
        "way",
        "case",
        "point",
    }
)


_GENERIC_CAPS = frozenset(
    {
        "works",
        "items",
        "tips",
        "ideas",
        "things",
        "stuff",
        "info",
        "data",
        "matter",
        "case",
        "list",
        "note",
        "notes",
        "topic",
        "topics",
    }
)


_PRONOUNS_AND_PARTICLES = frozenset(
    {
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "you",
        "your",
        "yours",
        "he",
        "him",
        "his",
        "she",
        "her",
        "hers",
        "it",
        "its",
        "they",
        "them",
        "their",
        "theirs",
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "doing",
        "done",
        "have",
        "has",
        "had",
        "having",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
    }
)


_GENERIC_ENTITY_FILTER = (
    _GENERIC_HEADS
    | _NON_SPECIFIC_ADJ
    | _CIRCUMSTANTIAL_MODS
    | _GENERIC_ENDINGS
    | _GENERIC_CAPS
    | _PRONOUNS_AND_PARTICLES
)


_RECORD_QUERY_STOPWORDS = frozenset(
    {
        *_PRONOUNS_AND_PARTICLES,
        "about",
        "again",
        "also",
        "answer",
        "current",
        "currently",
        "details",
        "find",
        "give",
        "know",
        "latest",
        "lookup",
        "please",
        "recent",
        "record",
        "records",
        "remember",
        "show",
        "tell",
        "using",
    }
)


try:
    from snowballstemmer import stemmer as _make_stemmer  # type: ignore[import-untyped]

    _PORTER_STEMMER: Any | None = _make_stemmer("english")
except ImportError:  # pragma: no cover — dep guaranteed in pyproject
    _PORTER_STEMMER = None


# Inflected forms whose base can't be derived by Snowball suffix rules.
_IRREGULAR_VERB_LEMMAS: dict[str, str] = {
    "ran": "run",
    "ate": "eat",
    "took": "take",
    "taken": "take",
    "said": "say",
    "told": "tell",
    "made": "make",
    "went": "go",
    "gone": "go",
    "saw": "see",
    "seen": "see",
    "got": "get",
    "gotten": "get",
    "had": "have",
    "did": "do",
    "done": "do",
    "was": "be",
    "were": "be",
    "been": "be",
    "thought": "think",
    "knew": "know",
    "known": "know",
    "brought": "bring",
    "bought": "buy",
    "paid": "pay",
    "met": "meet",
    "won": "win",
    "lost": "lose",
    "found": "find",
    "left": "leave",
    "felt": "feel",
    "kept": "keep",
    "held": "hold",
    "drove": "drive",
    "drank": "drink",
    "drunk": "drink",
    "sang": "sing",
    "sung": "sing",
    "swam": "swim",
    "swum": "swim",
    "wrote": "write",
    "written": "write",
    "spoke": "speak",
    "spoken": "speak",
    "broke": "break",
    "broken": "break",
    "chose": "choose",
    "chosen": "choose",
    "fell": "fall",
    "fallen": "fall",
    "rose": "rise",
    "risen": "rise",
    "began": "begin",
    "begun": "begin",
    "gave": "give",
    "given": "give",
    "sent": "send",
    "built": "build",
    "caught": "catch",
    "taught": "teach",
    "fought": "fight",
    "sought": "seek",
    "spent": "spend",
    "rode": "ride",
    "ridden": "ride",
    "stood": "stand",
    "understood": "understand",
    "ran_to": "run_to",
    "payed": "pay",
    "has": "have",
    "bred": "breed",
    # Force-collapse: Snowball stems these inconsistently otherwise.
    "experienced": "experience",
    "experiences": "experience",
    "experiencing": "experience",
}


def _stem_verb_head(word: str) -> str:
    """Reduce a verb head-word to its base form: irregular lookup, then Snowball."""
    if not word or len(word) < 2:
        return word
    if word in _IRREGULAR_VERB_LEMMAS:
        word = _IRREGULAR_VERB_LEMMAS[word]
    if _PORTER_STEMMER is None:
        return word
    return str(_PORTER_STEMMER.stemWord(word))


_AUX_VERBS = frozenset(
    {
        "is",
        "be",
        "are",
        "was",
        "were",
        "am",
        "been",
        "being",
        "has",
        "have",
        "had",
        "having",
        "do",
        "does",
        "did",
        "doing",
        "done",
        "can",
        "could",
        "should",
        "would",
        "will",
        "may",
        "might",
        "must",
        "shall",
        "get",
        "got",
        "gets",
        "getting",
    }
)


def verb_phrase(v: str) -> str:
    """A raw verb as the model wrote it, case-folded and underscore-joined, with
    leading auxiliaries stripped ("has_capital" -> "capital", "is_located_in" ->
    "located_in") and NOT stemmed: the readable attribute a memory key names.
    `canonicalize_verb` stems its head for matching relations."""
    key = fold_case(str(v or "").strip()).replace(" ", "_")
    while "_" in key:
        head, _, tail = key.partition("_")
        if head in _AUX_VERBS and tail:
            key = tail
        else:
            break
    return key


def canonicalize_verb(v: str) -> str:
    """Map a raw verb to its lemma via head-word stemming. Idempotent.

    Underscore-joined phrases ("is_interested_in") have leading auxiliaries
    stripped, the head stemmed, and the prepositional tail preserved
    ("interest_in"). Returns "" when only a pure copula remains — caller drops
    the triple, since the relation then carries no semantic signal.
    """
    key = verb_phrase(v)
    if not key:
        return ""
    if "_" in key:
        head, _, tail = key.partition("_")
        return _stem_verb_head(head) + "_" + tail
    stemmed = _stem_verb_head(key)
    if stemmed in _AUX_VERBS or _stem_verb_head(stemmed) in _AUX_VERBS:
        return ""
    return stemmed
