"""Graph-quality primitives for Loom's ClickHouse-backed entity plane.

These helpers keep graph write paths consistent without introducing a second
graph database. They produce stable canonical entity text/IDs, normalize
relation verbs, check namespace ontology membership, and encode the temporal
invalidation rule used by graph repair/reconciliation code.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_SPACE_RE = re.compile(r"\s+")
_VOWELS = frozenset("aeiouy")
_PUNCT_RE = re.compile(r"[^\w\s:/.-]+")
_ARTICLE_RE = re.compile(r"(?:the|a|an)\s+")
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
    # The closing (new_object) value's own valid_from; None when it is undated.
    new_valid_from: datetime | None = None


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
# What every first-person form canonicalises to.
SELF_SUBJECT = "user"
# The most triples one memory row states; the triple writer keeps the first ones.
MAX_TRIPLES_PER_ROW = 8

# Singularize the head noun so distinct-thing COUNTs don't split on
# plural/singular surface forms ("data breach" vs "data breaches", "kit" vs
# "kits" → off-by-one counts). Lightweight, dependency-free; only the LAST word
# is touched (multi-word entities + proper-noun heads are rarely plural). A
# singular head is itself singular, so a second pass changes nothing. A head
# without a vowel is an acronym (`https`, `dns`), never a plural.
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
        "gas",
        "alias",
        "atlas",
        "bias",
        "canvas",
        "lens",
        "physics",
        "mathematics",
        "economics",
        "diabetes",
        "kudos",
        "iris",
    }
)
_LONGEST_UNCHANGED = max(map(len, _SINGULAR_UNCHANGED))


def _lead(text: str) -> str:
    """`text` from past its leading dots, spaces and articles, which uncover each other
    (`the .the .net` -> `net`); one forward scan."""
    i = 0
    while True:
        while i < len(text) and text[i] in " .":
            i += 1
        article = _ARTICLE_RE.match(text, i)
        if article is None:
            return text[i:]
        i = article.end()


def _settle(text: str) -> str:
    """`text` without its trailing dots and spaces and with its last segment singular
    (`api responses` -> `api response`, `system.databases` -> `system.database`); a
    segment ends at a space or `./_-:`, so `node.js` and `foo.rs` keep their suffix."""
    end = len(text)
    while end and text[end - 1] in " .":
        end -= 1
    start = max(map(lambda sep: text.rfind(sep, 0, end), _SEGMENT_ENDS)) + 1
    vowel = any(text[i] in _VOWELS for i in range(start, end))
    cut = _singular_cut(text, start, end, vowel)
    if cut is None:
        return text[:end]
    end, suffix = cut
    return text[:end] + suffix


_SEGMENT_ENDS = " ./_-:"


def _singular_cut(text: str, s: int, e: int, has_vowel: bool) -> tuple[int, str] | None:
    """The singular of the word `text[s:e]` as (its new end, what follows it); None when
    it is singular already."""
    n = e - s
    if n <= 3 or not has_vowel or text.endswith("ss", s, e):
        return None
    if n <= _LONGEST_UNCHANGED and text[s:e] in _SINGULAR_UNCHANGED:
        return None
    if text.endswith("ies", s, e) and n > 4:
        return e - 3, "y"  # parties -> party
    if text.endswith(("ches", "shes", "xes"), s, e):
        return e - 2, ""  # breaches -> breach, boxes -> box
    if text.endswith(("ses", "zes"), s, e):
        # buses -> bus, classes -> class, buzzes -> buzz; else only the `s`
        # (responses -> response, sizes -> size)
        stem = n - 2
        kept = (stem <= _LONGEST_UNCHANGED and text[s : e - 2] in _SINGULAR_UNCHANGED) or (
            stem > 3 and text.endswith(("us", "is", "ss", "zz", "tz"), s, e - 2)
        )
        return (e - 2, "") if kept else (e - 1, "")
    if text.endswith("ves", s, e):
        return e - 3, "f"  # shelves -> shelf
    if text.endswith("s", s, e) and not text.endswith(("us", "is"), s, e):
        return e - 1, ""  # kits -> kit, doctors -> doctor
    return None


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


def _normalise(lowered: str) -> str:
    text = lowered.replace("'s", "").replace("’s", "")
    text = _PUNCT_RE.sub(" ", text)
    text = _settle(_lead(_SPACE_RE.sub(" ", text)))
    return SELF_SUBJECT if text in _FIRST_PERSON_FORMS else text


def canonical_entity(raw: str) -> CanonicalEntity:
    """Return stable canonical entity text + ID for storage/search.

    Collapses first-person forms (i / me / user / ...) to "user" to match the
    read side (graph seeds and query triples use `canonical_entity_text`); the original
    surface form is preserved in `aliases` for provenance and recall."""

    original = str(raw or "").strip()
    text = _normalise(original.lower())
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
) -> bool:
    """True when a newer fact should close an older open interval.

    If the existing edge is already closed, or the new fact has no real-world
    start time, there is nothing deterministic to invalidate.
    """

    if new_valid_at is None or existing_valid_to is not None:
        return False
    if existing_valid_at is None:
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


# How many days a fact's stated date may sit before the moment its statement was
# seen and still be that session's own day (a session crossing midnight, a date
# rounded to the day). The bulk write keeps a restatement's first date within it too.
SESSION_DATE_TOLERANCE_DAYS = 2


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
    earlier_batch_objects: Collection[str] = (),
    new_seen_at: datetime | None = None,
) -> list[IntervalRepair]:
    """Return deterministic triple closures for contradictory single-valued facts.

    A contradiction is deliberately narrow: same subject + relation, different
    object, open existing interval, on a relation the namespace declares (or
    the seed list treats as) single-valued. This captures current-state facts
    such as employer/location/status without asking an LLM to decide freshness.

    Ordering: of the new fact and an open existing value, the newer one stays
    current and the older one closes when the newer one starts.

    * A fact starts at its `valid_from`. The new fact, undated, starts at
      `arrival_at`, the transaction time of this write: an undated "status =
      live" is an observation about now. With neither, nothing happens.
    * Two facts with different starts order by start, so a late-arriving older
      value closes the new row, not the value the graph holds.
    * Of two facts with the same start, the later arrival is newer, by
      `last_seen` second, then by object text, so two writers racing on one date
      agree on the value that stays current. The new fact arrived at
      `new_seen_at` (its row's `last_seen`); a value earlier in this write
      (`earlier_batch_objects`) arrived before it.
    * An undated existing row starts when it was seen. It is newer when it was
      seen after both this write and the new fact's start (a replay dated at its
      write meets the writes since), or seen more than
      `SESSION_DATE_TOLERANCE_DAYS` after the new fact's date and not earlier in
      this write (a past fact told late: Rome from July, imported after London was
      stated in October). Seen at the new fact's start, it ties as above, so two
      undated writers racing in one second agree on one current value.
    * A dated and an undated value, either of them the new one, with the undated
      one seen within `SESSION_DATE_TOLERANCE_DAYS` after the dated one's start,
      are one session's statements: they order by arrival, as a tie does, so a
      dated and an undated writer racing each other agree too. Outside that
      window they order by start.
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
    earlier = {canonical_entity_text(o) for o in earlier_batch_objects}
    open_rows: list[tuple[dict[str, object], str]] = []
    for row in existing:
        old_subject = canonical_entity_text(str(row.get("subject") or ""))
        old_relation = canonical_relation(str(row.get("relation") or "")).verb
        old_object = canonical_entity_text(str(row.get("object") or ""))
        if old_subject != subject or old_relation != relation or old_object == obj:
            continue
        if old_object in skip or isinstance(row.get("valid_to"), datetime):
            continue
        open_rows.append((row, old_object))
    # Plain MergeTree keeps one open row per observation, and an edge is keyed by its
    # row's start. Each object gets one verdict from all its rows, so the new value is
    # never both closed and closing: it is the newer when any of its rows is, from the
    # earliest such takeover; otherwise every start of it closes when the new one starts.
    # Each row is judged by its own (valid_from, last_seen): when another row of the
    # value was seen says nothing about when this one was stated.
    observed: dict[str, list[tuple[datetime | None, datetime | None]]] = {}
    for row, old_object in open_rows:
        start, seen = row.get("valid_from"), row.get("last_seen")
        observed.setdefault(old_object, []).append(
            (
                start if isinstance(start, datetime) else None,
                seen if isinstance(seen, datetime) else None,
            )
        )
    repairs: list[IntervalRepair] = []
    for old_object, rows in observed.items():
        # When this object took over from the new one, and the start of the row that did.
        takeovers: list[tuple[datetime, datetime | None]] = []
        for valid_from, seen in dict.fromkeys(rows):
            # An undated row starts when seen. Two values with one start order by
            # arrival (this row's own: the tie is between two statements of that
            # start), and so do a dated and an undated one stated in one session.
            start = valid_from if valid_from is not None else seen
            by_arrival = start == effective_new or _one_session(
                valid_from, seen, new_valid_at, effective_new
            )
            arrived_later = (
                by_arrival
                and old_object not in earlier
                and _arrived_later(seen, old_object, new_seen_at, obj)
            )
            if valid_from is None:
                seen_since = bool(seen and arrival_at and seen > max(arrival_at, effective_new))
                told_late = bool(
                    seen
                    and new_valid_at is not None
                    and old_object not in earlier
                    and seen - new_valid_at > timedelta(days=SESSION_DATE_TOLERANCE_DAYS)
                )
                if seen and (seen_since or told_late or arrived_later):
                    takeovers.append((seen, valid_from))
            elif valid_from > effective_new or arrived_later:
                takeovers.append((valid_from, valid_from))
        if takeovers:
            # The row being written is already superseded: close the NEW row.
            newer_from, from_start = min(takeovers, key=lambda t: t[0])
            repairs.append(
                IntervalRepair(
                    subject=subject,
                    relation=relation,
                    old_object=obj,
                    new_object=old_object,
                    valid_to=newer_from,
                    valid_from=new_valid_at,
                    invalid_at=superseded_at,
                    new_valid_from=from_start,
                )
            )
            continue
        for valid_from in dict.fromkeys(start for start, _ in rows):
            repairs.append(
                IntervalRepair(
                    subject=subject,
                    relation=relation,
                    old_object=old_object,
                    new_object=obj,
                    valid_to=effective_new,
                    valid_from=valid_from,
                    invalid_at=superseded_at,
                    new_valid_from=new_valid_at,
                )
            )
    return repairs


def _one_session(
    row_from: datetime | None,
    row_seen: datetime | None,
    new_from: datetime | None,
    new_start: datetime,
) -> bool:
    """Whether, of a stored row and the new fact, one is dated and the other undated and
    the undated one was seen within `SESSION_DATE_TOLERANCE_DAYS` after the dated one's
    start: one session's two statements, which order by arrival, whichever is new."""
    if row_from is None and new_from is not None:
        dated, seen = new_from, row_seen
    elif row_from is not None and new_from is None:
        dated, seen = row_from, new_start
    else:
        return False
    if seen is None:
        return False
    return dated <= seen <= dated + timedelta(days=SESSION_DATE_TOLERANCE_DAYS)


def _arrived_later(seen: object, obj: str, than_seen: datetime | None, than_obj: str) -> bool:
    # `last_seen` is in whole seconds, as the writers stamp it; two rows in one second
    # order by object.
    if not isinstance(seen, datetime) or than_seen is None:
        return False
    return (seen, obj) > (than_seen, than_obj)


def restates_current(
    repair: IntervalRepair,
    valid_from: datetime | None,
    seen: datetime,
    *,
    closed_at: datetime,
    declared: Mapping[str, str],
) -> bool:
    """Whether a row of the value `repair` closed, stated again with `valid_from` and
    seen at `seen`, is newer than the value that closed it. The interval repair
    decides, as if that row were written now against the closing value. The closing
    value was seen at the close, or, undated, when the repair dated it (`valid_to`)."""
    closer: dict[str, object] = {
        "subject": repair.subject,
        "relation": repair.relation,
        "object": repair.new_object,
        "valid_from": repair.new_valid_from,
        "valid_to": None,
        "last_seen": closed_at if repair.new_valid_from is not None else repair.valid_to,
    }
    verdict = triple_interval_repairs(
        existing=[closer],
        new_subject=repair.subject,
        new_relation=repair.relation,
        new_object=repair.old_object,
        new_valid_at=valid_from,
        declared=declared,
        arrival_at=seen,
        new_seen_at=seen,
    )
    # The row is current when the verdict closes the closing value.
    return [r.old_object for r in verdict] == [repair.new_object]


def facet_triples(
    agent: str, fact_verbs: list[str], files: list[str], concepts: list[str], topics: list[str]
) -> Iterator[tuple[str, str, str]]:
    """The facet triples Loom mints for one entry, about its actor (the agent).
    Files and concepts become code nodes, so facts.traverse answers "what touches
    this file"; topics hang off `about`, so a topic never passes for a library."""
    subject = canonical_entity_text(agent or "user") or "user"
    for path in files:
        yield (
            subject,
            code_facet_relation("file", fact_verbs),
            canonical_entity_text(path) or path.lower(),
        )
    for concept in concepts:
        lowered = concept.lower()
        node = (
            lowered
            if lowered.startswith(("commit:", "error:"))
            else (canonical_entity_text(concept) or lowered)
        )
        yield subject, code_facet_relation(classify_code_concept(concept), fact_verbs), node
    for topic in topics:
        yield subject, "about", canonical_entity_text(topic) or topic.lower()
