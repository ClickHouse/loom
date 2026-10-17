"""The triple plane follows the row plane's supersession.

A knowledge update arrives as a memory ROW ("The chairperson of Fatah is
Moshe Kahlon") and retires its predecessor row under `newest_wins` — by the
templated-substitution rule, by the judge, or by the value-update linker.
The triple the predecessor carried, `(fatah, chairperson_of, mahmoud abbas)`,
is closed only when the update's OWN triple arrives under the same subject
and relation name. On multi-hop questions over updated facts most
misses had the update row active and its predecessor superseded while the
predecessor's triple stayed open — the extractor had keyed the update
`moshe_kahlon_chairperson`, named its relation differently, reversed it, or
emitted no triple at all. CURRENT FACTS then asserted the stale value under a
header saying superseded facts were excluded, and the reader believed it.

This is the canonicalization problem of open information extraction: one
relation, many surface forms. The fix here does not canonicalize relation names. It copies
them: the predecessor row's open triples are the relation names and
directions the graph already uses for that subject, so when the row is
superseded, every open triple the row attests that the successor row does
not is closed, and — when the two rows differ by one substituted span — the
successor triple is written under the predecessor's subject and relation
with the new span as the object. Pure functions here; the ClickHouse side
lives in `memory.app.entity_projections`.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from server.memory.domain.graph_quality import (
    SELF_SUBJECT,
    canonical_edge_pair,
    fold_case,
    is_single_valued_relation,
)
from server.memory.domain.triple_provenance import FACET

Triple = tuple[str, str, str]


_LEAD_INS: tuple[tuple[str, ...], ...] = (
    ("user", "stated", "that"),
    ("user", "said", "that"),
    ("user", "mentioned", "that"),
    ("user", "noted", "that"),
    ("the", "user", "stated", "that"),
    ("the", "user", "said", "that"),
)


# Spacing aside, what is never a word of its own.
_NOT_SIGNS = frozenset(".,;:?!_")
# A curly apostrophe is the straight one, so "don’t" and "don't" are one word.
_APOSTROPHES = str.maketrans("‘’", "''")


def _scan(text: str, *, signs: bool, dots: bool = False) -> list[str]:
    """The words of `text`: a letter or digit, then letters, digits, combining marks
    (a vowel sign is part of its word), apostrophes and hyphens. With `dots`, a "."
    between two letters or digits is inside the word ("next.js"), unless every part is
    one letter ("U.S."). With `signs`, any other mark but spacing,
    punctuation and "_" is a word of its own. Scripts written without spaces are one
    word per run."""
    words: list[str] = []
    word = ""
    chars = unicodedata.normalize("NFC", text or "").translate(_APOSTROPHES)
    for n, c in enumerate(chars):
        if c.isalnum() or (word and (c in "'-" or unicodedata.category(c).startswith("M"))):
            word += c
            continue
        # "next.js", "x.ai", "ASP.NET", "v3.14". A full stop missing its space
        # ("end.Then") glues two words too: a rare typo.
        if dots and c == "." and word[-1:].isalnum() and chars[n + 1 : n + 2].isalnum():
            word += c
            continue
        if word:
            words.extend(_unless_initials(word))
            word = ""
        if signs and not c.isspace() and c not in _NOT_SIGNS:
            words.append(c)
    if word:
        words.extend(_unless_initials(word))
    return words


def _unless_initials(word: str) -> list[str]:
    # "U.S" and "e.g" are abbreviations, one word per letter, not a dotted name;
    # "3.9" is a version.
    parts = word.split(".")
    initials = len(parts) > 1 and all(len(p) == 1 and p.isalpha() for p in parts)
    return parts if initials else [word]


def split_words(text: str, *, dots: bool = False) -> list[str]:
    """The words of `text` as written, without the apostrophes and hyphens that
    end them. With `dots`, a dotted name ("socket.io", "config.yaml") is one word."""
    return [t.strip("'-") for t in _scan(text, signs=False, dots=dots) if t.strip("'-")]


def _tokens(text: str) -> list[str]:
    return _without_lead_in(split_words(fold_case(text)))


def _without_lead_in(toks: list[str]) -> list[str]:
    # The coverage sweep's facts lead with "User stated that …"; the first
    # pass's do not. The lead-in is not part of the shape:
    # "Thomas Arne was born in the city of London" and "User stated that
    # Thomas Arne was born in the city of Bengaluru" are one pair.
    for lead in _LEAD_INS:
        if len(toks) > len(lead) and tuple(toks[: len(lead)]) == lead:
            toks = toks[len(lead) :]
            break
    return toks


def entity_in(text: str, entity: str) -> bool:
    """Whether `entity` is named in `text`, tolerating the backfill's stems:
    the graph spells `mahmoud abbas` as `mahmoud abba` and `the beatles` as
    `beatle`, so each entity token must be a prefix of some text token (and
    at least three characters long, or an exact match)."""
    return _named(_tokens(text), entity)


def _named(words: Sequence[str], entity: str) -> bool:
    """`entity_in` over a text's `_tokens`."""
    ents = [t for t in _tokens(entity) if len(t) >= 2]
    return bool(ents) and all(
        any(w == e or (len(e) >= 3 and w.startswith(e)) for w in words) for e in ents
    )


def substituted_spans(old: str, new: str) -> tuple[str, str] | None:
    """The one span that differs between two statements of the same shape:
    ("the chairperson of fatah is mahmoud abbas", "… is moshe kahlon") →
    ("mahmoud abbas", "moshe kahlon"). None when the texts differ in more
    than one place, share fewer than two tokens, or one is a prefix of the
    other."""
    a, b = _tokens(old), _tokens(new)
    if not a or not b or a == b:
        return None
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    # The shape is shared when the common frame is at least half the shorter
    # text and the fills are short spans — an entity name, a value — not a
    # rewritten clause ("was born in the city of London" / "born and raised
    # in Bengaluru, Karnataka" differ in more than a fill).
    if i + j < max(2, min(len(a), len(b)) // 2):
        return None
    old_mid, new_mid = a[i : len(a) - j], b[i : len(b) - j]
    if not old_mid or not new_mid or len(old_mid) > 5 or len(new_mid) > 5:
        return None
    return " ".join(old_mid), " ".join(new_mid)


_COPULA = frozenset({"is", "are", "was", "were", "be", "been", "has", "have", "had"})
# Words that make the phrase before a copula an attribute of something ("the
# capital of Italy") rather than a bare name. A possessive does too ("Italy's
# capital"): see `_names_an_attribute`.
_ATTRIBUTE_PHRASE_WORDS = frozenset({"the", "of", "for", "in", "at", "on", "a", "an"})
_PREPOSITIONS = frozenset({"in", "at", "on", "of", "for", "to", "from", "with", "by", "near"})
# The words an object follows ("lives in | London", "is | Rome").
_BEFORE_AN_OBJECT = _PREPOSITIONS | _COPULA
# The words that introduce an object ("drives a | red sports car").
_ARTICLES = frozenset({"a", "an", "the"})
# The copulas and "have" verbs of the major languages, for the bare-name rule
# ("Wang Xiaoming est âgé de 28 ans" / "… est ingénieur logiciel", "У меня есть …").
_ANY_COPULA = _COPULA | frozenset(
    {
        "est", "sont", "es", "son", "está", "están", "ist", "sind", "hat", "haben",
        "è", "sono", "ha", "é", "são", "tem", "är", "har", "er", "zijn", "heeft",
        "есть", "является", "являются",
    }
)  # fmt: skip


def _names_an_attribute(words: list[str]) -> bool:
    # A possessive is one word ("italy's"): curly apostrophes are straight by now.
    # It counts only with a noun after it ("italy's capital"). As the last word it
    # is a name ("macy's", "mcdonald's").
    if any(w in _ATTRIBUTE_PHRASE_WORDS for w in words):
        return True
    return any(w.endswith("'s") for w in words[:-1])


def object_substitution(old: str, new: str) -> tuple[str, str] | None:
    """`substituted_spans` narrowed to an update of one attribute: the statements share
    a beginning of three tokens or more and differ only in the object that ends them
    ("I work at Acme in London" / "… at Globex in London" → ("acme", "globex")). The
    shapes it refuses are under "Words" in docs/architecture/memory-architecture.md."""
    a, b = _tokens(old), _tokens(new)
    if len(a) < 4 or len(b) < 4 or a == b:
        return None
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    if i < 3 or i >= len(a) or i >= len(b):
        return None
    old_mid, new_mid = a[i:], b[i:]
    if len(old_mid) > 5 or len(new_mid) > 5:
        return None
    if _COPULA & set(old_mid) or _COPULA & set(new_mid):
        return None
    # A shared ending of two words or more, after a beginning that does not end in a
    # preposition, copula or article, is likely a predicate's object in any language
    # ("The user currently lives in London" / "… works in London", "… живёт в Москве"
    # / "… работает в Москве"): another fact, not an update. A missed update is safer
    # than a wrong supersession. One shared word is the head of the object ("prefers
    # dark mode" / "… light mode"), and so are more after an article ("drives a red
    # sports car" / "… a blue sports car"). Otherwise a shared ending from a
    # preposition on is a detail both keep, and the object is the span before it.
    end = 0
    while end < min(len(old_mid), len(new_mid)) and old_mid[-1 - end] == new_mid[-1 - end]:
        end += 1
    if end > 1 and a[i - 1] not in _BEFORE_AN_OBJECT | _ARTICLES:
        return None
    detail = [n for n in range(len(old_mid) - end, len(old_mid)) if old_mid[n] in _PREPOSITIONS]
    if detail:
        cut = len(old_mid) - detail[0]
        old_mid, new_mid = old_mid[:-cut], new_mid[:-cut]
        if not old_mid or not new_mid:
            return None
    # A beginning that ends at the copula names the attribute only when the
    # words before it are an attribute phrase ("the capital of Italy is …",
    # "the chairperson of Fatah is …"). After a bare name ("Wang Xiaoming is |
    # 28 years old" / "… is | a software engineer") the differing spans are two
    # attributes, not an update, unless both are a value of one kind (numbers).
    if (
        a[i - 1] in _ANY_COPULA
        and not _names_an_attribute(a[: i - 1])
        and not (old_mid[0][:1].isdigit() and new_mid[0][:1].isdigit())
    ):
        return None
    return " ".join(old_mid), " ".join(new_mid)


@dataclass(frozen=True)
class RowSupersession:
    loser_text: str
    winner_text: str
    winner_key: str = ""
    winner_valid_from: Any = None
    # The live winner's id, `content_sha256` and writer, which its successor triples
    # are linked to.
    winner_id: str = ""
    winner_sha256: str = ""
    winner_user_id: str = ""
    # When the loser's own row was replaced.
    loser_valid_to: datetime | None = None
    # The triples each row's current value stated (`entity_triple_rows`). Empty for a
    # row written before rows were linked: its triples are then found by its text.
    loser_triples: frozenset[Triple] = frozenset()
    winner_triples: frozenset[Triple] = frozenset()
    # Successors the follow wrote for each row (inferred links); they add to what the
    # row attests.
    loser_inferred: frozenset[Triple] = frozenset()
    winner_inferred: frozenset[Triple] = frozenset()

    # Read once: the follow matches every candidate triple against them.
    @functools.cached_property
    def loser_words(self) -> tuple[str, ...]:
        return tuple(_tokens(self.loser_text))

    @functools.cached_property
    def winner_words(self) -> tuple[str, ...]:
        return tuple(_tokens(self.winner_text))


class Successor(NamedTuple):
    """A triple to write in a closed one's place: the winner's key and date, the
    closed predecessor's confidence and provenance, and the winner's id, value hash
    and writer to link it to."""

    subject: str
    relation: str
    object: str
    memory_key: str
    valid_from: Any
    confidence: float
    provenance: str
    memory_id: str = ""
    content_sha256: str = ""
    user_id: str = ""

    @property
    def triple(self) -> Triple:
        return (self.subject, self.relation, self.object)


@dataclass(frozen=True)
class FollowPlan:
    # (subject, relation, object) of the open triples to close
    closes: list[Triple]
    inserts: list[Successor]
    # When each closed triple stopped holding: the latest `loser_valid_to` closing it.
    closed_at: dict[Triple, Any] = field(default_factory=dict)
    # Triples a live winner still states: no pair closes or ends them.
    kept: set[Triple] = field(default_factory=set)
    # When each triple a loser's row linked stopped holding there, open or not: the
    # latest `loser_valid_to` of the losers linking it.
    ends: dict[Triple, datetime | None] = field(default_factory=dict)


def _later(a: Any, b: Any) -> Any:
    """The later of two close dates; None is now, later than any stored date."""
    return None if a is None or b is None else max(a, b)


def still_states(text: str, triple: Triple) -> bool:
    """A row's value names `triple`: the object, and the subject too unless it is the
    writer (a first-person value never spells it)."""
    return _still_named(_tokens(text), triple)


def _still_named(words: Sequence[str], triple: Triple) -> bool:
    subject, _, obj = triple
    return _named(words, obj) and (subject == SELF_SUBJECT or _named(words, subject))


# A revised value keeps a successor only when it says nothing the value it was
# written for did not: its sentences are the old value's, in order, each whole or
# cut to a statement of the triple, and no statement is dropped. Anything added
# closes it, since words alone cannot tell detail from a retraction ("..., near the
# river" from "..., until May"): a closed triple comes back with the next write that
# states it, a wrongly open one never leaves.
# A sentence ends at a mark, maybe closed by a quote or bracket, followed by a space or
# the end: "next.js" and "3.12" go on.
_CLOSERS = "\"')]\u2019\u201d\u00bb"
_CLOSER = f"[{re.escape(_CLOSERS)}]"
# Python's lookbehind is fixed-width: up to two closers ("?")") after the mark.
_SENTENCE_END_RE = re.compile(
    rf"(?:(?<=[.;?!])|(?<=[.;?!]{_CLOSER})|(?<=[.;?!]{_CLOSER}{_CLOSER}))(?=\s|$)"
)
_CLAUSE_BREAK_RE = re.compile(r"[,:]")
_CONJUNCTIONS = frozenset({"and", "but"})


class _Sentence(NamedTuple):
    clauses: list[list[str]]
    asks: bool  # it ends in a question mark


def _words_and_signs(text: str) -> list[str]:
    # An added "❌" or quote is an addition too.
    return _scan(fold_case(text), signs=True)


def _protect_names(text: str, triple: Triple) -> str:
    """`text` with the commas and dots inside the triple's subject and object blanked,
    so "Austin, Texas" and "St. Louis" are never split. Words match as in `_mentions`."""
    text = fold_case(text)
    for name in (triple[0], triple[2]):
        words = _words_and_signs(name)
        if len(words) < 2:
            continue
        parts = [re.escape(w) + (r"\w*" if len(w) >= 3 else "") for w in words]
        pattern = r"(?<!\w)" + r"[\s.,]+".join(parts) + r"(?!\w)"
        text = re.sub(pattern, lambda m: re.sub(r"[.,]", " ", m.group()), text)
    return text


def _sentences(text: str, triple: Triple) -> list[_Sentence]:
    """The sentences of `text`, each as its clauses of words. A clause ends at a comma
    or colon, and at "and" or "but", but never inside the triple's subject or object
    ("Austin, Texas", "Marks and Spencer"). The text's lead-in ("User stated that") is
    dropped."""
    keep = set(_words_and_signs(triple[0])) | set(_words_and_signs(triple[2]))
    breaks = _CONJUNCTIONS - keep
    out: list[_Sentence] = []
    for raw in _SENTENCE_END_RE.split(_protect_names(text, triple)):
        asks = raw.rstrip().rstrip(_CLOSERS).endswith("?")
        clauses: list[list[str]] = []
        for part in _CLAUSE_BREAK_RE.split(raw):
            clause: list[str] = []
            words = _words_and_signs(part)
            if not out and not clauses:
                words = _without_lead_in(words)
            for word in words:
                if word in breaks:
                    if clause:
                        clauses.append(clause)
                    clause = []
                else:
                    clause.append(word)
            if clause:
                clauses.append(clause)
        if clauses:
            out.append(_Sentence(clauses, asks))
        elif asks and out:
            # "London... ?": the question mark ends the sentence before it.
            out[-1] = out[-1]._replace(asks=True)
    return out


def _mentions(clause: list[str], entity: list[str]) -> bool:
    """`entity` appears in `clause` as consecutive words, each matched by prefix as
    in `entity_in`."""
    n = len(entity)
    return n > 0 and any(
        all(
            w == e or (len(e) >= 3 and w.startswith(e))
            for w, e in zip(clause[i : i + n], entity, strict=True)
        )
        for i in range(len(clause) - n + 1)
    )


def _relation_words(triple: Triple) -> list[str]:
    return [w for w in triple[1].split("_") if len(w) >= 3]


def _clauses_saying(text: str, triple: Triple) -> list[list[str]]:
    """The clauses of `text` naming the triple's object with its relation's words: "work
    in London" of "I live in Rome and work in London" says `(user, work_in, london)`."""
    obj = _words_and_signs(triple[2])
    relation_words = _relation_words(triple)
    return [
        clause
        for sentence in _sentences(text, triple)
        for clause in sentence.clauses
        if _mentions(clause, obj) and any(w.startswith(r) for w in clause for r in relation_words)
    ]


def _says_in_one_clause(text: str, triple: Triple) -> bool:
    return bool(_clauses_saying(text, triple))


def _still_says(pair: RowSupersession, triple: Triple) -> bool:
    # A clause the update left word for word still says the triple; a changed one
    # ("I no longer work in London") does not.
    was = _clauses_saying(pair.loser_text, triple)
    return any(clause in was for clause in _clauses_saying(pair.winner_text, triple))


def _statements(sentences: list[_Sentence], triple: Triple) -> dict[int, list[list[str]]]:
    """The statements of `triple`, by sentence: each sentence naming the object that
    does not ask, cut after the clause naming it or the first with the relation's
    words, whichever comes later. A sentence without the relation's words is a
    statement only whole. Statements with the relation's words are preferred."""
    obj = _words_and_signs(triple[2])
    relation_words = _relation_words(triple)

    def has_relation(clause: list[str]) -> bool:
        return any(w.startswith(r) for w in clause for r in relation_words)

    found, preferred = {}, {}
    for i, sentence in enumerate(sentences):
        if sentence.asks:
            continue
        named = [n for n, c in enumerate(sentence.clauses) if _mentions(c, obj)]
        if not named:
            continue
        related = [n for n, c in enumerate(sentence.clauses) if has_relation(c)]
        if related:
            preferred[i] = sentence.clauses[: max(named[0], related[0]) + 1]
        else:
            found[i] = sentence.clauses
    return preferred or found


def restates(old: str, new: str, triple: Triple) -> bool:
    """`new` says nothing `old` did not and still states the triple: its sentences
    are `old`'s, in order, each whole or cut to a statement of the triple
    (`_statements`), and no statement is dropped. "I live in London, not Paris" is
    restated by "I live in London" and "I live in London, not Paris."; not by "I
    live in London, near the river", "Still, I live in London", "I live in
    London?" or "I live in Rome and visited London"."""
    was, now = _sentences(old, triple), _sentences(new, triple)
    statements = _statements(was, triple)
    if not statements:
        return False
    used = 0  # sentences of `new` matched so far
    for i, sentence in enumerate(was):
        cut = statements.get(i)
        here = now[used] if used < len(now) else None
        if here == sentence or (here is not None and here.clauses == cut and not here.asks):
            used += 1
        elif cut is not None:
            return False  # a statement was dropped
    return used == len(now)  # and nothing new is left over


def _gainsaid(
    stated: frozenset[Triple],
    triple: Triple,
    declared: Mapping[str, str] | None,
) -> bool:
    """What a row's extractor stated says otherwise: the triple's single-valued
    relation holds another object ("lives in Rome"). Another relation to the same
    object ("works in London") is another fact, not a contradiction."""
    subject, relation, obj = triple
    return is_single_valued_relation(relation, declared) and any(
        s == subject and r == relation and o != obj for s, r, o in stated
    )


def carried(
    text: str,
    sha256: str,
    stated: frozenset[Triple],
    written: Mapping[Triple, Mapping[str, str]],
    declared: Mapping[str, str] | None = None,
) -> frozenset[Triple]:
    """The triples written or stated for a row that its value (`text`, hashed
    `sha256`) holds now. `written` maps each to the hashes of the values it was written
    or stated for, and those values' text. One written for this value was judged
    against it by the follow that wrote it. One for a value the row was revised from holds
    while the value says nothing the old one did not and still states it
    (`restates`), and nothing its extractor stated for this value says otherwise."""
    words = _tokens(text)
    return frozenset(
        t
        for t, olds in written.items()
        if sha256 in olds
        or t in stated
        or (
            _still_named(words, t)
            and not _gainsaid(stated, t, declared)
            and any(restates(old, text, t) for old in olds.values() if old)
        )
    )


def _names(words: Sequence[str], triple: Triple) -> bool:
    # Stricter than `still_states`: prose is read for a triple only when it names both
    # ends, so a first-person triple of a row written before links is never read from it.
    return _named(words, triple[0]) and _named(words, triple[2])


def _attests(
    words: Sequence[str],
    stated: frozenset[Triple],
    inferred: frozenset[Triple],
    triple: Triple,
) -> bool:
    """A row attests the successors written for it, and what its extractor stated;
    a row whose extractor stated nothing is read by its text instead."""
    if triple in inferred:
        return True
    return triple in stated if stated else _names(words, triple)


def _loser_attests(pair: RowSupersession, triple: Triple) -> bool:
    return _attests(pair.loser_words, pair.loser_triples, pair.loser_inferred, triple)


def _winner_attests(pair: RowSupersession, triple: Triple) -> bool:
    return _attests(pair.winner_words, pair.winner_triples, pair.winner_inferred, triple)


def plan_follow(open_triples: list[dict[str, Any]], pairs: list[RowSupersession]) -> FollowPlan:
    """Which open triples each superseded row attests and its successor does
    not, and which successor triples the substituted span yields.

    A row attests the successors written for it and the triples its extractor
    stated; a row whose extractor stated nothing is read by its text, which names
    both a triple's subject and object. The winner attesting it too, or saying it in a
    clause the update left unchanged, means the update kept that value: nothing
    closes, nor does a triple another pair's winner still attests. A successor is
    written only when the pair differs by exactly one span and that span is the
    closed triple's object or subject — the relation name and direction are the
    predecessor's. A successor already open is
    listed too, for the winner's links to it; the writer does not write it again.
    Facet edges are the agent's, not a row's: no retirement closes them."""
    have_open = {
        (str(t.get("subject", "")), str(t.get("relation", "")), str(t.get("object", "")))
        for t in open_triples
    }
    kept = {t for t in have_open if any(_winner_attests(p, t) for p in pairs)}
    closes: list[Triple] = []
    inserts: list[Successor] = []
    seen_close: set[Triple] = set()
    closed_at: dict[Triple, Any] = {}
    seen_insert: set[tuple[Triple, str, str]] = set()
    for pair in pairs:
        spans = substituted_spans(pair.loser_text, pair.winner_text)
        for t in open_triples:
            s, r, o = (
                str(t.get("subject", "")),
                str(t.get("relation", "")),
                str(t.get("object", "")),
            )
            if not s or not r or not o or t.get("provenance") == FACET:
                continue
            if not _loser_attests(pair, (s, r, o)):
                continue
            if _winner_attests(pair, (s, r, o)):
                continue  # the successor still attests it
            if _still_says(pair, (s, r, o)):
                kept.add((s, r, o))
                continue
            if (s, r, o) not in seen_close:
                seen_close.add((s, r, o))
                closes.append((s, r, o))
                closed_at[(s, r, o)] = pair.loser_valid_to
            else:
                # Each row that stated it held until its own replacement: the latest wins.
                closed_at[(s, r, o)] = _later(closed_at[(s, r, o)], pair.loser_valid_to)
            if not spans:
                continue
            old_mid, new_mid = spans
            conf = float(t.get("confidence") or 1.0)
            succ: Triple | None = None
            if entity_in(old_mid, o) or entity_in(o, old_mid):
                succ = (s, r, new_mid)
            elif entity_in(old_mid, s) or entity_in(s, old_mid):
                succ = (new_mid, r, o)
            # The winner still says it: the span substituted was another fact's object.
            if succ is None or _says_in_one_clause(pair.winner_text, (s, r, o)):
                continue
            # Open already or not, the winner's key and row are named as its source, so
            # the winner's retirement closes it whatever else the winner's row states.
            if (succ, pair.winner_key, pair.winner_id) in seen_insert:
                continue
            seen_insert.add((succ, pair.winner_key, pair.winner_id))
            inserts.append(
                Successor(
                    *succ,
                    memory_key=pair.winner_key,
                    valid_from=pair.winner_valid_from,
                    confidence=conf,
                    provenance=str(t.get("provenance") or ""),
                    memory_id=pair.winner_id,
                    content_sha256=pair.winner_sha256,
                    user_id=pair.winner_user_id,
                )
            )
    closes = [t for t in closes if t not in kept]
    closed_at = {t: at for t, at in closed_at.items() if t not in kept}
    ends: dict[Triple, datetime | None] = {}
    for pair in pairs:
        for linked in pair.loser_triples | pair.loser_inferred:
            end = pair.loser_valid_to
            ends[linked] = _later(ends[linked], end) if linked in ends else end
            # One not open ("until December") the winner still says is kept as well.
            if _winner_attests(pair, linked) or _still_says(pair, linked):
                kept.add(linked)
    return FollowPlan(closes=closes, inserts=inserts, closed_at=closed_at, kept=kept, ends=ends)


def merge_plans(plans: list[FollowPlan], kept: frozenset[Triple] = frozenset()) -> FollowPlan:
    """One plan for several: a triple any of them keeps, `kept` names (a live row
    outside the follow still states it), or writes as a live row's successor, is
    not closed; one several close ends at the latest date; and each successor is
    listed once per winner key and row."""
    all_kept = set(kept) | {t for p in plans for t in p.kept}
    never_closed = all_kept | {s.triple for p in plans for s in p.inserts}
    closes: list[Triple] = []
    closed_at: dict[Triple, Any] = {}
    inserts: list[Successor] = []
    written: set[tuple[Triple, str, str]] = set()
    ends: dict[Triple, datetime | None] = {}
    for plan in plans:
        for t, end in plan.ends.items():
            ends[t] = _later(ends[t], end) if t in ends else end
        for t in plan.closes:
            if t in never_closed:
                continue
            if t in closed_at:
                closed_at[t] = _later(closed_at[t], plan.closed_at.get(t))
            else:
                closes.append(t)
                closed_at[t] = plan.closed_at.get(t)
        for ins in plan.inserts:
            if (ins.triple, ins.memory_key, ins.memory_id) not in written:
                written.add((ins.triple, ins.memory_key, ins.memory_id))
                inserts.append(ins)
    return FollowPlan(closes=closes, inserts=inserts, closed_at=closed_at, kept=all_kept, ends=ends)


def edge_ends(
    gone: list[Triple], held: frozenset[Triple], rows: Sequence[Mapping[str, Any]], now: Any
) -> list[tuple[str, str, Any]]:
    """Where the edges of the `gone` triples end: with the latest end of their stored
    `rows`, now at the earliest. An edge is just a pair of entities, so one a `held`
    triple still valid keeps ("works in London" keeps "lives in London" dropped) does
    not end, nor does one with a gone triple still open."""
    dropped = set(gone)
    kept: set[tuple[str, str]] = set()
    found: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        s, rel, o = str(r["subject"]), str(r["relation"]), str(r["object"])
        if (s, rel, o) in held and (r["valid_to"] is None or r["valid_to"] > now):
            kept.add(canonical_edge_pair(s, o))
        if (s, rel, o) in dropped:
            found.setdefault(canonical_edge_pair(s, o), []).append(r["valid_to"])
    return sorted(
        (a, b, max([now, *ends]))
        for (a, b), ends in found.items()
        if (a, b) not in kept and None not in ends
    )
