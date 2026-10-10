"""Graph-quality primitives for Loom's ClickHouse-backed entity plane.

These helpers keep graph write paths consistent without introducing a second
graph database. They produce stable canonical entity text/IDs, normalize
relation verbs, check namespace ontology membership, and encode the temporal
invalidation rule used by graph repair/reconciliation code.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s:/.-]+")
_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_SINGLE_VALUED_RELATIONS = {
    "works_at",
    "employed_by",
    "employer",
    "has_employer",
    "lives_in",
    "lives_at",
    "located_in",
    "located_at",
    "based_in",
    "current_role",
    "has_role",
    "role_at",
    "status",
    "relationship_status",
    "primary_email",
    "phone_number",
    "home_address",
    # Biographical and organisational attributes that hold one value at a
    # time (birthplace, citizenship, headquarters, spouse, position, ...);
    # fact-update benchmarks are built from exactly these, and the
    # job/location-shaped list above closes none of them on its own. Verbs
    # with real multiplicity — founder, author, associated_with, educated_at
    # — stay out: a co-author is not a contradiction.
    "born_in",
    "birthplace",
    "died_in",
    "death_place",
    "place_of_death",
    "citizen_of",
    "citizenship",
    "nationality",
    "headquartered_in",
    "headquarters",
    "capital",
    "capital_of",
    "ceo",
    "chief_executive_officer",
    "head_of_government",
    "head_of_state",
    "married_to",
    "spouse",
    "plays_position",
    "created_in",
    # Functional roles and one-at-a-time affiliations: the office has one holder at a
    # time, the team one sport, the person one religion. These are the verbs the
    # relation backfill names, so the list has to hold the `_of` forms it emits. Still
    # out: author, founder, developer, producer, performer, educated_at, language — a
    # co-author or a second language is not a contradiction.
    "ceo_of",
    "chairperson",
    "chairperson_of",
    "director_of",
    "head_of",
    "prime_minister_of",
    "president_of",
    "position",
    "associated_with_sport",
    "sport",
    "affiliated_with_religion",
    "religion",
    "broadcast_by",
    "original_broadcaster",
}
# NOTE "address" is deliberately absent. Stemming folds the verb "addresses" ("Maya
# Angelou addresses identity", "Astrakhan addresses poverty") onto the same lemma as the
# postal-address noun, and most equal-date single-valued ties on "address" are that verb
# — only the strictly-older rule keeps them from being wrongly closed. A namespace that
# stores postal addresses declares the verb's cardinality (namespace.register_ontology
# cardinality="one"); the seed list is a default, not the rule.


# Code-native edge relations. These ride the EXISTING entity_triples /
# entity_edges tables — no new graph store. They make "what fixed this error",
# "what touches this file", "what shipped in this commit" first-class multi-hop
# queries over the same WITH RECURSIVE facts.traverse path. Closed-class on
# purpose: a coding agent's tool actions map onto exactly these verbs.
CODE_EDGE_RELATIONS = frozenset(
    {"imports", "modifies", "fixes", "causes", "blocked_by", "references", "touches"}
)

# Code entity-node kinds. A facet string is tagged with one of these when
# emitted as a graph node so traversal can scope by kind ("only file nodes
# touched by session X"). Kind is encoded as a "<kind>:" prefix on the node
# text ONLY for error/commit (whose surfaces would otherwise collide with
# prose); file + function nodes keep their natural surface (the path / call)
# so a query seed lands on the same node without knowing the kind.
CODE_NODE_KINDS = frozenset({"file", "function", "error", "library", "commit"})


@dataclass(frozen=True)
class CanonicalEntity:
    text: str
    entity_id: str
    aliases: list[str] = field(default_factory=list)


def classify_code_concept(concept: str) -> str:
    """Classify a deterministically-extracted code concept into a node kind.

    Pure, dependency-free, deterministic — the write path uses it to decide
    the relation verb (an error concept gets a `fixes`/`causes` edge; a
    symbol/library gets `references`). Returns one of CODE_NODE_KINDS minus
    'file' (files are classified by their own facet array, not here).
    Defaults to 'function' for a call-shaped or snake/camel identifier,
    'library' for a bare lowercase package-ish token, 'error' for an
    error/exception token, 'commit' for a "commit:<sha>" surface.
    """
    c = str(concept or "").strip()
    if not c:
        return "function"
    if c.lower().startswith("commit:"):
        return "commit"
    # Error / exception types (CamelCase failure suffix) or screaming codes.
    if re.search(
        r"(Error|Exception|Warning|Fault|Failure|Timeout|Panic|Overflow|Underflow)$",
        c,
    ) or re.fullmatch(r"E[A-Z]{3,}|[A-Z]{2,}_[A-Z_]{2,}", c):
        return "error"
    # Dotted symbol ("psycopg2.connect") or a call-shaped / mixed-case
    # identifier → a function/symbol node.
    if "." in c or any(ch.isupper() for ch in c[1:]) or "_" in c:
        return "function"
    # A bare lowercase token is more likely a library / package name.
    return "library"


def code_facet_relation(kind: str, fact_verbs: list[str]) -> str:
    """Pick the code edge relation for a (subject -> facet-node) edge.

    `fact_verbs` are the canonical relation verbs the extractor already
    emitted for the parent fact (e.g. "fixed", "broke"). When a fact verb
    is itself a code relation we honor it; otherwise we fall back to the
    kind's natural verb so the edge is still typed and traversable:
      error -> 'fixes' if the fact reads as a fix, else 'causes'
      file  -> 'modifies'
      commit -> 'references'
      function/library -> 'references'
    """
    verbs = {str(v).strip().lower() for v in (fact_verbs or [])}
    explicit = verbs & CODE_EDGE_RELATIONS
    if explicit:
        # Honor the most specific explicit code verb the fact carried.
        for pref in (
            "fixes",
            "causes",
            "modifies",
            "imports",
            "blocked_by",
            "references",
            "touches",
        ):
            if pref in explicit:
                return pref
    if kind == "file":
        return "modifies"
    if kind == "error":
        # Heuristic: a fact whose verbs read like a resolution gets 'fixes',
        # otherwise the error is something the work 'causes'/encountered.
        _fix_verbs = {
            "fix",
            "fixed",
            "resolve",
            "resolved",
            "patch",
            "patched",
            "null_check",
            "null_checked",
        }
        if verbs & _fix_verbs:
            return "fixes"
        return "causes"
    if kind == "commit":
        return "references"
    return "references"


@dataclass(frozen=True)
class CanonicalRelation:
    verb: str
    accepted: bool
    confidence: float


@dataclass(frozen=True)
class IntervalRepair:
    subject: str
    relation: str
    old_object: str
    new_object: str
    valid_to: datetime
    valid_from: datetime | None = None
    # The TRANSACTION-TIME moment this older fact was SUPERSEDED by the
    # contradicting newer one. Distinct from valid_to (the older fact's own
    # stated end = the newer fact's start). NULL when the supersession-axis write
    # does not populate it — the writer only sets
    # entity_triples/entity_edges.invalid_at when this is populated.
    invalid_at: datetime | None = None


# First-person collapse for the SINGLE canonical entity normalizer — read
# (graph seeds, LLM triples) and write (write_entity_edges /
# write_entity_triples) both route through canonical_entity_text, so a
# first-person node and its query seed land on the same text. Forms are
# post-article-strip, so "the user" also lands here. "you"/"yourself" are
# deliberately excluded: they depend on speaker context (assistant "you" =
# user, user "you" = assistant); collapsing both directions to "user"
# costs ~8 points on single-session assistant questions.
_FIRST_PERSON_FORMS = frozenset(
    {"i", "me", "myself", "user", "the user", "the customer", "customer", "my self"}
)

# Singularize the head noun so distinct-thing COUNTs don't split on
# plural/singular surface forms ("data breach" vs "data breaches", "kit" vs
# "kits" → off-by-one counts). Lightweight, dependency-free; only the LAST word
# is touched (multi-word entities + proper-noun heads are rarely plural).
# Changing canonical_entity reshapes the whole graph plane, so re-ingest and
# re-bench before relying on this.
_SINGULAR_UNCHANGED = frozenset(
    {
        "series",
        "species",
        "news",
        "data",
        "analysis",
        "status",
        "bus",
        "lens",
        "physics",
        "mathematics",
        "economics",
        "diabetes",
        "kudos",
        "iris",
    }
)


def _singularize_head(text: str) -> str:
    if not text or " " in text and not text.rsplit(" ", 1)[-1]:
        return text
    head = text.rsplit(" ", 1)[-1] if " " in text else text
    prefix = text[: len(text) - len(head)]
    w = head
    if len(w) <= 3 or w in _SINGULAR_UNCHANGED or w.endswith("ss"):
        return text
    if w.endswith("ies") and len(w) > 4:
        s = w[:-3] + "y"  # parties -> party
    elif w.endswith(("ches", "shes", "xes", "zes", "ses")):
        s = w[:-2]  # breaches -> breach, boxes -> box
    elif w.endswith("ves"):
        s = w[:-3] + "f"  # shelves -> shelf
    elif w.endswith("s") and not w.endswith(("us", "is", "ss")):
        s = w[:-1]  # kits -> kit, doctors -> doctor
    else:
        return text
    return prefix + s


# Tokens that are never an entity, whatever the corpus — pronouns and function words,
# interjections and discourse markers, and the fragments a contraction leaves once
# punctuation is stripped ("i m", "can t"). Corpus-generic nouns ("test", "column",
# "session") are NOT here: what is generic in a CI namespace is a subject in another,
# and the degree-based hub drop handles them per graph. This list plus the two-character
# floor touches roughly 5–30% of a graph's entity edges, depending on the corpus.
_LEXICAL_JUNK = frozenset(
    """i me my mine myself you your yours yourself we us our ours ourselves they them
    their theirs themselves he him his she her hers it its this that these those the a an
    who whom whose which what where when why how here there
    and or but so if then than because while although though yet nor
    is are was were be been being am do does did done have has had having
    can could will would shall should may might must
    not no yes yeah yep nope ok okay sure fine well also just only very really quite
    maybe perhaps probably actually basically literally definitely
    wow hey hi hello bye oh ah uh um hmm huh lol haha yay ugh oops
    cool awesome great nice good bad glad happy sorry thank thanks please welcome
    someone something anything nothing everything everyone anyone somebody nobody
    now today tonight tomorrow yesterday later soon ago
    all any each every some many much more most less few several both either neither
    same other another such own
    one two three first second last next previous new old
    user users assistant none null true false n a""".split()
    # Light verbs and their inflections: "fix", "share", "say", "add", "like",
    # "got", "check", "keep", "let", "look" and "make" can rank among the
    # best-connected "entities" of a graph (tens of thousands of edges each).
    # A verb is never an entity in any corpus; domain nouns stay off the list.
    + """fix fixed fixes fixing add added adding share shared sharing say said saying
    like liked got get gets getting check checked keep kept let look looked looking make
    made making see saw seen know knew known think thought want wanted need needed go
    went going come came take took taken give gave given tell told ask asked find found
    finding try tried tries use used using work worked working help helped feel felt put
    start started stop stopped end ended show showed showing mean meant seem seemed
    happen happened become became remain include continue change changed move moved
    turn run ran play played read write wrote watch watched listen eat drink sleep sit
    stand open opened close closed buy bought sell sold pay paid spend spent hold held
    carry bring brought send sent receive received lose lost win won meet met visit
    call called text texted email emailed post posted comment commented update updated
    hope hoped wish love loved hate enjoy enjoyed remember forget forgot plan planned
    decide decided agree agreed understand understood learn learned""".split()
)
_CONTRACTION_FRAGMENT_RE = re.compile(
    r"^(?:i|you|we|they|he|she|it|that|there|who|what|let|can|don|doesn|didn|isn|aren|wasn|"
    r"weren|won|wouldn|couldn|shouldn|hasn|haven|hadn|ain|gonna|wanna)\s+(?:m|s|re|ve|ll|d|t)$"
)


def is_lexical_junk_entity(text: str) -> bool:
    """True for a node that can never carry meaning as an entity: shorter
    than three characters, a function word or interjection,
    or a contraction fragment. Takes the canonical text; a caller that has
    not canonicalised should pass `canonical_entity_text(raw)`."""
    t = str(text or "").strip().lower()
    if len(t) < 3:
        return True
    if t in _LEXICAL_JUNK:
        return True
    if _CONTRACTION_FRAGMENT_RE.match(t):
        return True
    # a phrase made only of junk words ("i m really") is junk too
    words = t.split()
    return len(words) > 1 and all(w in _LEXICAL_JUNK or len(w) < 3 for w in words)


def canonical_entity(raw: str) -> CanonicalEntity:
    """Return stable canonical entity text + ID for storage/search.

    Collapses first-person forms (i / me / user / ...) to "user" to match the
    read side (graph seeds and query triples use `canonical_entity_text`); the original
    surface form is preserved in `aliases` for provenance and recall."""

    original = str(raw or "").strip()
    lowered = original.lower()
    lowered = lowered.replace("'s", "").replace("’s", "")
    lowered = _ARTICLE_RE.sub("", lowered)
    lowered = _PUNCT_RE.sub(" ", lowered)
    text = _SPACE_RE.sub(" ", lowered).strip(" .")
    text = _singularize_head(text)
    if text in _FIRST_PERSON_FORMS:
        text = "user"
    entity_id = text.replace(" ", "_")
    return CanonicalEntity(text=text, entity_id=entity_id, aliases=[original] if original else [])


def canonical_entity_text(raw: str) -> str:
    """The one entity normalizer for writes and reads: strips articles,
    possessives and punctuation and folds first-person forms to "user", so a
    query seed and the stored node land on identical text. Idempotent."""
    return canonical_entity(raw).text


def canonical_entities(values: Iterable[str]) -> list[str]:
    """`values` in canonical form, in order, each once: what a row's `entities` holds, so
    a search's canonical entity filter meets the stored text."""
    return list(dict.fromkeys(c for v in values if (c := canonical_entity_text(v))))


def canonical_relation(
    raw: str,
    *,
    accepted_verbs: set[str] | None = None,
    default_confidence: float = 1.0,
) -> CanonicalRelation:
    """Normalize a relation verb and validate against an optional ontology."""

    verb = str(raw or "").strip().lower().replace(" ", "_")
    verb = re.sub(r"[^a-z0-9_]+", "_", verb)
    verb = re.sub(r"_+", "_", verb).strip("_")
    accepted_set = {v.strip().lower() for v in (accepted_verbs or set()) if v.strip()}
    accepted = not accepted_set or verb in accepted_set
    confidence = float(default_confidence if accepted else min(default_confidence, 0.5))
    return CanonicalRelation(verb=verb, accepted=accepted, confidence=confidence)


def should_invalidate_interval(
    *,
    existing_valid_at: datetime | None,
    existing_valid_to: datetime | None,
    new_valid_at: datetime | None,
    tie_closes: bool = False,
) -> bool:
    """True when a newer fact should close an older open interval.

    If the existing edge is already closed, or the new fact has no real-world
    start time, there is nothing deterministic to invalidate.

    `tie_closes` — when both facts start at the same instant, the existing
    one (which arrived first) closes. The default keeps both; the
    single-valued triple writer passes True because the
    extractor stamps the session date on every fact it cannot date more
    precisely, so "same day, different value" is the common shape of a
    state change, not a coincidence.
    """

    if new_valid_at is None or existing_valid_to is not None:
        return False
    if existing_valid_at is None:
        return True
    if tie_closes and existing_valid_at == new_valid_at:
        return True
    return existing_valid_at < new_valid_at


_SINGLE_VALUED_LEMMAS: set[str] | None = None


def _lemma_relation(raw: str) -> str:
    """The relation exactly as the triple WRITE path stores it:
    canonicalize_verb (head-word lemma) then canonical_relation.
    Lazy import — search_text imports this module."""
    from server.memory.domain.search_text import canonicalize_verb

    return canonical_relation(canonicalize_verb(raw)).verb


CARDINALITY_ONE = "one"
CARDINALITY_MANY = "many"


def declared_cardinality(entries: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Fold (relation_verb, cardinality) ontology rows into LEMMA space.

    The writer stores lemmatized verbs, so a declaration made against the
    surface form ("works_at") must be probed with the stored form
    ("work_at"). Blank or unknown cardinalities are dropped: an undeclared
    verb falls through to the seed list, never to a guess."""
    out: dict[str, str] = {}
    for name, card in entries:
        c = str(card or "").strip().lower()
        if c not in (CARDINALITY_ONE, CARDINALITY_MANY):
            continue
        out[_lemma_relation(str(name))] = c
    return out


def is_single_valued_relation(raw: str, declared: Mapping[str, str] | None = None) -> bool:
    """Whether a relation should auto-close older different objects.

    Membership is checked in LEMMA space: stored triples carry lemmatized
    verbs ("works_at" -> "work_at"), and this gate is probed with those
    stored forms — a surface-form set would never match, so interval
    repair would never fire for exactly the relations listed here.

    `declared` — the namespace's own relation cardinalities
    (:func:`declared_cardinality`), consulted FIRST: a verb declared
    ``many`` is multi-valued even if the seed list says otherwise, and a verb
    declared ``one`` is single-valued whatever its surface.
    """
    global _SINGLE_VALUED_LEMMAS
    if _SINGLE_VALUED_LEMMAS is None:
        _SINGLE_VALUED_LEMMAS = {_lemma_relation(v) for v in _SINGLE_VALUED_RELATIONS}
    relation = _lemma_relation(raw)
    if declared:
        card = declared.get(relation)
        if card == CARDINALITY_ONE:
            return True
        if card == CARDINALITY_MANY:
            return False
    if relation in _SINGLE_VALUED_LEMMAS:
        return True
    return any(
        relation.endswith(suffix)
        for suffix in (
            "_employer",
            "_status",
            "_location",
            "_address",
            "_role",
        )
    )


def canonical_edge_pair(entity_a: str, entity_b: str) -> tuple[str, str]:
    """Return the storage-order pair for an undirected entity edge."""

    a = canonical_entity_text(entity_a)
    b = canonical_entity_text(entity_b)
    return (a, b) if a <= b else (b, a)


def triple_interval_repairs(
    *,
    existing: list[dict[str, object]],
    new_subject: str,
    new_relation: str,
    new_object: str,
    new_valid_at: datetime | None,
    superseded_at: datetime | None = None,
    declared: Mapping[str, str] | None = None,
    arrival_at: datetime | None = None,
    later_batch_objects: Collection[str] = (),
) -> list[IntervalRepair]:
    """Return deterministic triple closures for contradictory single-valued facts.

    A contradiction is deliberately narrow: same subject + relation, different
    object, open existing interval, on a relation the namespace declares (or
    the seed list treats as) single-valued. This captures current-state facts
    such as employer/location/status without asking an LLM to decide freshness.

    Ordering:

    * The new fact is ordered by `new_valid_at`; when it carries no real-world
      date, by `arrival_at` — the transaction time of this write. An undated
      "status = live" is an observation about now, so it closes the dated
      "status = in progress" from last week. With neither, nothing happens.
    * An existing fact that starts STRICTLY EARLIER, or at the SAME instant
      (the extractor stamps the session date on everything it cannot date
      more precisely), is closed at the new fact's start.
    * An existing fact that starts LATER means the NEW fact is the stale one
      — a late-arriving older value. The repair then closes the new row at
      the existing fact's start, so the graph never shows two current values.
    * `later_batch_objects` — objects that a LATER triple in the same write
      asserts for this subject + relation. Those rows are skipped here (the
      later write's own repair closes this one), so within one batch the
      last-written value survives instead of every value closing every other.

    `superseded_at` is the TRANSACTION-TIME moment the
    contradiction was written; it is stamped onto each repair's `invalid_at`
    (the supersession axis, distinct from valid_to = the older fact's stated
    end). None leaves invalid_at unwritten.
    """

    effective_new = new_valid_at if new_valid_at is not None else arrival_at
    if effective_new is None:
        return []
    subject = canonical_entity_text(new_subject)
    relation = canonical_relation(new_relation).verb
    if not is_single_valued_relation(relation, declared):
        return []
    obj = canonical_entity_text(new_object)
    skip = {canonical_entity_text(o) for o in later_batch_objects}
    repairs: list[IntervalRepair] = []
    for row in existing:
        old_subject = canonical_entity_text(str(row.get("subject") or ""))
        old_relation = canonical_relation(str(row.get("relation") or "")).verb
        old_object = canonical_entity_text(str(row.get("object") or ""))
        if old_subject != subject or old_relation != relation or old_object == obj:
            continue
        if old_object in skip:
            continue
        existing_valid_at = row.get("valid_from")
        existing_valid_to = row.get("valid_to")
        if not isinstance(existing_valid_at, datetime):
            existing_valid_at = None
        if not isinstance(existing_valid_to, datetime):
            existing_valid_to = None
        if existing_valid_to is not None:
            continue
        if should_invalidate_interval(
            existing_valid_at=existing_valid_at,
            existing_valid_to=existing_valid_to,
            new_valid_at=effective_new,
            tie_closes=True,
        ):
            repairs.append(
                IntervalRepair(
                    subject=subject,
                    relation=relation,
                    old_object=old_object,
                    new_object=obj,
                    valid_to=effective_new,
                    valid_from=existing_valid_at,
                    invalid_at=superseded_at,
                )
            )
        elif existing_valid_at is not None and existing_valid_at > effective_new:
            # Late-arriving older value: the row being written is already
            # superseded by a fact the graph holds. Close the NEW row.
            repairs.append(
                IntervalRepair(
                    subject=subject,
                    relation=relation,
                    old_object=obj,
                    new_object=old_object,
                    valid_to=existing_valid_at,
                    valid_from=new_valid_at,
                    invalid_at=superseded_at,
                )
            )
    return repairs
