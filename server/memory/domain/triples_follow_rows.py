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

import re
from dataclasses import dataclass
from typing import Any

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")


_LEAD_INS: tuple[tuple[str, ...], ...] = (
    ("user", "stated", "that"),
    ("user", "said", "that"),
    ("user", "mentioned", "that"),
    ("user", "noted", "that"),
    ("the", "user", "stated", "that"),
    ("the", "user", "said", "that"),
)


def _tokens(text: str) -> list[str]:
    toks = [t.strip("'-") for t in _TOKEN_RE.findall((text or "").lower()) if t.strip("'-")]
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
    ents = [t for t in _tokens(entity) if len(t) >= 2]
    if not ents:
        return False
    words = _tokens(text)
    for e in ents:
        if not any(w == e or (len(e) >= 3 and w.startswith(e)) for w in words):
            return False
    return True


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
# capital of Italy", "Italy's capital") rather than a bare name.
_ATTRIBUTE_PHRASE_WORDS = frozenset({"the", "of", "s", "for", "in", "at", "on", "a", "an"})


def object_substitution(old: str, new: str) -> tuple[str, str] | None:
    """`substituted_spans` narrowed to the update shape: the two statements
    share their whole beginning (subject and predicate, three tokens or
    more) and differ only in the span that ends them — the object. This is
    what an update to a single-valued attribute looks like, and what two
    statements about DIFFERENT subjects do not: "Basketball was created in
    the country of Soviet Union" / "Club Nouveau was created in the country
    of Soviet Union" share their ending, not their beginning; "The capital of
    Soviet Union is Moscow" / "The capital of Italy is Rome" share a
    beginning but the differing span carries the copula. A looser rule pairs
    both inside one write and supersedes the update row itself (basketball →
    Soviet Union), then closes its triple, and multi-hop questions lose their
    chain."""
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
    # A beginning that ends at the copula names the attribute only when the
    # words before it are an attribute phrase ("the capital of Italy is …",
    # "the chairperson of Fatah is …"). After a bare name ("Wang Xiaoming is |
    # 28 years old" / "… is | a software engineer") the differing spans are two
    # attributes, not an update, unless both are a value of one kind (numbers).
    if (
        a[i - 1] in _COPULA
        and not (_ATTRIBUTE_PHRASE_WORDS & set(a[: i - 1]))
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


@dataclass(frozen=True)
class FollowPlan:
    # (subject, relation, object) of the open triples to close
    closes: list[tuple[str, str, str]]
    # (subject, relation, object, winner_key, valid_from, confidence, provenance)
    # the confidence and provenance are the closed predecessor's
    inserts: list[tuple[str, str, str, str, Any, float, str]]


def plan_follow(open_triples: list[dict[str, Any]], pairs: list[RowSupersession]) -> FollowPlan:
    """Which open triples each superseded row attests and its successor does
    not, and which successor triples the substituted span yields.

    A triple is attested by a text when both its subject and its object are
    named in it. The winner naming the object too means the update kept that
    value (an elaboration): nothing closes. A successor is written only when
    the pair differs by exactly one span and that span is the closed
    triple's object or subject — the relation name and direction are the
    predecessor's. A successor already open in the graph is not repeated."""
    have_open = {
        (str(t.get("subject", "")), str(t.get("relation", "")), str(t.get("object", "")))
        for t in open_triples
    }
    closes: list[tuple[str, str, str]] = []
    inserts: list[tuple[str, str, str, str, Any, float, str]] = []
    seen_close: set[tuple[str, str, str]] = set()
    seen_insert: set[tuple[str, str, str]] = set()
    for pair in pairs:
        spans = substituted_spans(pair.loser_text, pair.winner_text)
        for t in open_triples:
            s, r, o = (
                str(t.get("subject", "")),
                str(t.get("relation", "")),
                str(t.get("object", "")),
            )
            if not s or not r or not o:
                continue
            if not (entity_in(pair.loser_text, s) and entity_in(pair.loser_text, o)):
                continue
            if entity_in(pair.winner_text, s) and entity_in(pair.winner_text, o):
                continue  # the successor still attests it
            if (s, r, o) not in seen_close:
                seen_close.add((s, r, o))
                closes.append((s, r, o))
            if not spans:
                continue
            old_mid, new_mid = spans
            conf = float(t.get("confidence") or 1.0)
            succ: tuple[str, str, str] | None = None
            if entity_in(old_mid, o) or entity_in(o, old_mid):
                succ = (s, r, new_mid)
            elif entity_in(old_mid, s) or entity_in(s, old_mid):
                succ = (new_mid, r, o)
            if succ and succ not in have_open and succ not in seen_insert:
                seen_insert.add(succ)
                prov = str(t.get("provenance") or "")
                inserts.append((*succ, pair.winner_key, pair.winner_valid_from, conf, prov))
    return FollowPlan(closes=closes, inserts=inserts)
