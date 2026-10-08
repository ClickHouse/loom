"""Deterministic query-intent parsing for memory.search.

Grammar-level classifier for formulaic English question shapes (counts,
sums, date spans, chronological ordering, latest-value lookups,
relative-date resolution). It emits the same signal tuple the enrichment
LLM produces so the handler can feed the existing planner / aggregate
machinery unchanged, and abstains (returns ``None``) on anything outside
the grammar so the LLM tier remains the fallback.

Patterns encode English interrogative structure, never corpus- or
workload-specific phrasing. "how many times did I visit X" counts event
rows; "how many X do I have now" asks for the latest stated value of a
changing attribute — routing the second to a COUNT computes a wrong
answer, so it is classified ``stated_value_update`` instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTHS: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_WORD_NUMBERS: dict[str, int] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "couple": 2,
    "few": 3,
}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(_WEEKDAYS)
_NUM_WORD_ALT = "|".join(_WORD_NUMBERS)

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# "March 15", "Mar 15th, 2023"
_MDY_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b")
# "15 March", "15th of March 2023"
_DMY_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b\.?(?:,?\s+(\d{{4}}))?"
)
# "March 2024" — month-precision; resolved to the first of the month.
_MY_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{4}})\b")
_AGO_RE = re.compile(rf"\b(\d+|{_NUM_WORD_ALT})\s+(?:of\s+)?(day|week|month|year)s?\s+ago\b")
_LAST_WEEKDAY_RE = re.compile(rf"\b(?:last|this past|past)\s+({_WEEKDAY_ALT})\b")
# "the past weekend" / "this past month" / "the previous year" resolve like
# "last …"; reading only "last weekend" misses a question whose store holds
# the Sunday event.
_LAST_PERIOD_RE = re.compile(
    r"\b(?:last|this past|past|previous)\s+(week|month|year|weekend|night)\b"
)

_FIRST_PERSON_RE = re.compile(r"\b(i|my|me|we|our|mine)\b")

# --- classification grammars ------------------------------------------------

# date-span: "how many days/weeks ... between/since/until/after/from"
_DATE_DIFF_HEAD_RE = re.compile(
    r"\bhow\s+(?:many|much)\s+(?:days?|weeks?|months?|years?|hours?|time|long)\b"
)
_DATE_DIFF_LINK_RE = re.compile(
    r"\b(?:between|since|until|till|after|elapsed|apart|passed|from\b.+\bto)\b"
)
# "how many days ago did I ..." — span from the event to observation_date.
_AGO_TAIL_RE = re.compile(r"\bhow\s+many\s+(?:days?|weeks?|months?|years?)\s+ago\b")
# "(for) how long had I been X when/after I Y" — duration head + first-person
# reference clause naming a second event. Scoped to the clause so a bare
# "how long" can't trip it.
_DURATION_WHEN_RE = re.compile(r"\b(?:for\s+)?how\s+long\b.*\b(?:when|after)\b\s+(?:i|we)\s+\w+")
# Third-person span shapes. Questions about a conversation name the
# person, so the first-person grammars above do not fire on them; these
# give the compute leg — the only thing that subtracts dates — its operands:
#   "how long did it take (for) X to Y"         → X began Y′ / X finished Y′
#   "how long did X V (for) before Y"           → X V / X Y
#   "how long did NP take" / "how long was NP"  → NP started / NP ended
_DURATION_TAKE_RE = re.compile(
    r"\bhow\s+long\s+did\s+it\s+take\s+(?:for\s+)?(.+?)\s+to\s+(.+)$", re.I
)
_DURATION_BEFORE_RE = re.compile(
    r"\bhow\s+long\s+(?:did|had|has|have|were|was)\s+(.+?)\s+(?:for\s+)?before\s+(.+)$", re.I
)
_DURATION_NP_RE = re.compile(
    r"\bhow\s+long\s+(?:did\s+(.+?)\s+(?:take|last)|was\s+(.+?)(?:\s+in\s+total)?)\s*$", re.I
)
# A completion verb at the head of the "to Y" clause names the END of the
# activity; the start probe drops it ("finish writing her book" → the thing
# that was begun is "writing her book").
_ORDINALS = frozenset(
    {"first", "second", "third", "fourth", "fifth", "last", "next", "previous", "initial", "final"}
)
_COMPLETION_HEAD_RE = re.compile(
    r"^(?:finish(?:ing)?|complete(?:ing)?|get\s+done\s+with|wrap\s+up|end)\s+", re.I
)

# Money-flow verbs the SUM aggregate sums over. The predicate handed to
# fetch_derived_aggregate is the query verb itself, which
# expand_predicate_class widens to its stored stem. A bare "how much money
# do I have" carries no flow verb and falls through to the stated-value
# branch below.
_MONEY_OUTFLOW = r"spend|spent|spending|pay|paid|cost|costs"
_MONEY_INFLOW = (
    r"raise|raised|earn|earned|save|saved|donate|donated|collect|collected"
    r"|make|made|give|gave|given|contribute|contributed|pledge|pledged"
)
_MONEY_VERB_ALT = f"{_MONEY_OUTFLOW}|{_MONEY_INFLOW}"
_SUM_RE = re.compile(
    rf"(?:\bhow much\b[^.;]*\b(?:{_MONEY_VERB_ALT})\b"
    r"|\btotal\s+(?:amount|money|cost|sum)\b"
    rf"|\btotal\b[^.;]*\b(?:{_MONEY_VERB_ALT})\b)"
)
# Extract (verb, object/category) from a SUM-shaped query: the query's own
# money-flow verb plus its trailing prepositional object ("on rent").
_SUM_VERB_OBJ_RE = re.compile(
    rf"\b({_MONEY_VERB_ALT})\b(?:\s+[\w$.,]+)*?\s+"
    r"(?:on|for|from|to|towards?|into)\s+"
    r"(?:the\s+|my\s+|our\s+|a\s+|an\s+)?([a-z][a-z \-']{2,40})"
)

_COUNT_TIMES_RE = re.compile(
    r"\bhow many times\s+(?:have|has|had|did|do|does|am|is|are|was|were)\s+(?:i|we)\b\s*(.*)$"
)
_COUNT_RE = re.compile(
    r"\bhow many\b(?!\s+(?:days?|weeks?|months?|years?|hours?|minutes?|times)\b)"
)
_COUNT_NP_RE = re.compile(
    r"\bhow many\s+([a-z][a-z \-']{1,40}?)\s+"
    r"(?:have|has|had|did|do|does|am|is|are|was|were)\s+(?:i|we)\b\s*(.*)$"
)
# Relative-clause count without subject-aux inversion ("how many books I
# read"). Tried only when _COUNT_NP_RE misses; pronoun + following verb token
# required so it can't swallow a bare "how many X".
_COUNT_NP_REL_RE = re.compile(
    r"\bhow many\s+([a-z][a-z \-']{1,40}?)\s+(?:i|we)\s+"
    r"(?:have|has|had|did|do|does)?\s*([a-z].*)$"
)
# Present-tense state: "how many X do I (currently) have/own ..."
_STATED_VALUE_RE = re.compile(
    r"\bhow many\s+(.{1,60}?)\s+(?:do|does)\s+(?:i|we)\s+"
    r"(?:currently\s+|now\s+)?(?:have|own|possess|hold|keep)\b"
)
# Stated inventory: "how many X are (there) in/on my ..."
_STATED_INVENTORY_RE = re.compile(
    r"\bhow many\s+(.{1,60}?)\s+(?:are|is)\s+(?:there\s+)?"
    r"(?:currently\s+|now\s+)?(?:in|on|at)\s+(?:my|our|the)\b"
)

_ORDER_FIRST_RE = re.compile(r"\b(first|earlier|earliest)\b")
_ORDER_LAST_RE = re.compile(r"\b(last|latest|later|most recently)\b")
_WHEN_RE = re.compile(r"^(?:when|what\s+(?:date|day|year|month|time))\b")
_RECENCY_RE = re.compile(
    r"\b(?:currently|current|now|nowadays|these days|at the moment|right now"
    r"|latest|most recent(?:ly)?|as of (?:today|now))\b"
)
_PREFERENCE_RE = re.compile(
    r"\b(?:what|which)\s+([a-z][a-z ]{1,30}?)\s+(?:do|does|am|are|is)\s+(?:i|we|my)\b"
    r".*\b(?:use|using|prefer|preferred|drive|driving|drink|drinking)\b"
)

_QUOTED_RE = re.compile(r"[\"“”]([^\"“”]{2,60})[\"“”]")
_CAP_SPAN_RE = re.compile(r"\b([A-Z][a-zA-Z0-9'&.-]*(?:\s+[A-Z][a-zA-Z0-9'&.-]*)+)\b")
_CAP_WORD_RE = re.compile(r"\b([A-Z][a-z]{2,})\b")

_DETERMINERS = {"the", "a", "an", "my", "our", "some", "any", "to"}


@dataclass(frozen=True)
class QueryIntent:
    """The deterministic parser's reading of a query.

    ``enrichment_signal()`` is the search's intent-signal tuple;
    ``derived_intent()`` matches `extract_derived_intent_llm`'s dict (or None
    when the parser lacks the operands).
    """

    query_shape: str = ""
    op: str = ""  # count | sum | date_span | latest | ""
    stated_value_update: bool = False
    resolved_dates: tuple[str, ...] = ()
    recency_intent: bool = False
    chronological_intent: str = ""  # asc | desc | ""
    preference_attribute: str = ""
    subject: str = "user"
    predicate: str = ""
    category: str = ""
    object_text: str = ""
    entities: tuple[str, ...] = ()
    sub_queries: tuple[str, ...] = ()
    # Operand probe texts for a date-difference. Two event clauses for
    # "between A and B"; one event clause for "how many days ago did I X",
    # in which case `diff_anchor_to_observation` is True (second operand is
    # the observation date). Each probe is embedded and matched against the
    # dated facts. Empty → the compute leg abstains (fail-open).
    diff_operands: tuple[str, ...] = ()
    diff_anchor_to_observation: bool = False

    def temporal_diff_spec(self) -> dict[str, object] | None:
        """Operand spec for the deterministic two-event dateDiff leg.

        Returns ``{operands: [text, ...], to_observation: bool}`` for a
        date-difference shape with a resolvable operand probe, else ``None``
        (the compute leg abstains, reader falls back to recalled hits). The
        interval arithmetic is done server-side with ``dateDiff``, never by
        the reader.
        """
        if self.query_shape != "temporal_diff" or self.op != "date_span":
            return None
        operands = [o for o in self.diff_operands if o]
        if self.diff_anchor_to_observation:
            if len(operands) != 1:  # event vs observation date
                return None
        elif len(operands) != 2:  # two-event diff needs both probes
            return None
        return {
            "operands": operands,
            "to_observation": self.diff_anchor_to_observation,
        }

    def enrichment_signal(
        self,
    ) -> tuple[list[str], str, bool, str, list[str], list[str], str]:
        return (
            list(self.resolved_dates),
            self.chronological_intent,
            self.recency_intent,
            self.preference_attribute,
            list(self.entities),
            list(self.sub_queries),
            self.query_shape,
        )

    def derived_intent(self) -> dict[str, str] | None:
        # Stated-value updates are answered by the latest value, not a row count.
        if self.stated_value_update:
            return None
        if self.op not in ("count", "sum", "date_span"):
            return None
        # Without an operand dimension the aggregate would span every derived
        # row in the namespace — abstain to the LLM.
        if not (self.predicate or self.category or self.object_text):
            return None
        return {
            "op": self.op,
            "subject": self.subject,
            "predicate": self.predicate,
            "category": self.category,
            "object": self.object_text,
        }


def _parse_obs(observation_date: str | None) -> date | None:
    if not observation_date:
        return None
    try:
        return date.fromisoformat(observation_date.strip()[:10])
    except ValueError:
        return None


def _month_num(name: str) -> int | None:
    name = name.lower().rstrip(".")
    return _MONTHS.get(name) or _MONTHS.get(name[:3])


def resolve_relative_dates(query: str, observation_date: str | None = None) -> list[str]:
    """Resolve absolute and relative date expressions to ISO dates.

    Relative forms (today / yesterday / last <weekday> / last
    week|month|year / N days|weeks|months|years ago / this past
    <weekday>) anchor on `observation_date`; without an anchor only
    absolute forms (ISO, month-name + day + year) resolve. Yearless
    month-day forms resolve to the most recent occurrence on or before
    the anchor. First-appearance order, deduplicated, capped at 4.
    """
    q = (query or "").lower()
    if not q:
        return []
    obs = _parse_obs(observation_date)
    found: list[tuple[int, str]] = []

    def add(pos: int, d: date) -> None:
        found.append((pos, d.isoformat()))

    for m in _ISO_DATE_RE.finditer(q):
        try:
            add(m.start(), date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            continue

    def _add_month_day(pos: int, mo: int, day: int, year_s: str | None) -> None:
        if year_s:
            try:
                add(pos, date(int(year_s), mo, day))
            except ValueError:
                pass
            return
        if obs is None:
            return  # yearless dates need an anchor
        try:
            candidate = date(obs.year, mo, day)
        except ValueError:
            return
        if candidate > obs:
            try:
                candidate = date(obs.year - 1, mo, day)
            except ValueError:
                return
        add(pos, candidate)

    covered: list[tuple[int, int]] = []
    for m in _MDY_RE.finditer(q):
        mo = _month_num(m[1])
        if mo:
            _add_month_day(m.start(), mo, int(m[2]), m[3])
            covered.append(m.span())
    for m in _DMY_RE.finditer(q):
        mo = _month_num(m[2])
        if mo:
            _add_month_day(m.start(), mo, int(m[1]), m[3])
            covered.append(m.span())
    for m in _MY_RE.finditer(q):
        # Skip month-year spans already consumed by a month-day-year form
        # ("march 15, 2023" must not also resolve as "march 2023").
        if any(s <= m.start() < e for s, e in covered):
            continue
        mo = _month_num(m[1])
        if mo:
            try:
                add(m.start(), date(int(m[2]), mo, 1))
            except ValueError:
                pass

    if obs is not None:
        mm = re.search(r"\btoday\b|\btonight\b", q)
        if mm:
            add(mm.start(), obs)
        mm = re.search(r"\bday before yesterday\b", q)
        if mm:
            add(mm.start(), obs - timedelta(days=2))
        else:
            mm = re.search(r"\byesterday\b", q)
            if mm:
                add(mm.start(), obs - timedelta(days=1))
        for m in _LAST_WEEKDAY_RE.finditer(q):
            wd = _WEEKDAYS[m[1]]
            # Most recent such weekday strictly before the anchor.
            add(m.start(), obs - timedelta(days=(obs.weekday() - wd - 1) % 7 + 1))
        for m in _LAST_PERIOD_RE.finditer(q):
            unit = m[1]
            if unit == "week":
                add(m.start(), obs - timedelta(days=7))
            elif unit == "month":
                add(m.start(), obs - timedelta(days=30))
            elif unit == "year":
                add(m.start(), obs - timedelta(days=365))
            elif unit == "weekend":
                # Most recent Saturday strictly before the anchor.
                add(m.start(), obs - timedelta(days=(obs.weekday() - 5 - 1) % 7 + 1))
            elif unit == "night":
                add(m.start(), obs - timedelta(days=1))
        for m in _AGO_RE.finditer(q):
            n_raw = m[1]
            n = int(n_raw) if n_raw.isdigit() else _WORD_NUMBERS.get(n_raw, 0)
            if n <= 0 or n > 5000:
                continue
            unit = m[2]
            days = n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
            add(m.start(), obs - timedelta(days=days))

    found.sort(key=lambda t: t[0])
    out: list[str] = []
    for _, iso in found:
        if iso not in out:
            out.append(iso)
        if len(out) >= 4:
            break
    return out


def _clean_np(s: str | None) -> str:
    s = (s or "").strip(" ,.?!'\"")
    s = re.sub(r"^(?:the|a|an|my|our|some|any|of)\s+", "", s)
    # Drop count-quantifier qualifiers ("how many DISTINCT cities") — they're
    # interrogative quantity words, not part of the stored category surface.
    s = re.sub(r"^(?:distinct|different|unique|separate|total)\s+", "", s)
    s = re.sub(
        r"\s+(?:so far|in total|altogether|overall|to date|this year|last year)$",
        "",
        s,
    )
    return s.strip()[:40]


def _clean_clause(s: str | None) -> str:
    """Normalize an event clause into an embeddable probe.

    Drops the leading subject pronoun ("i launched X" → "launched X"), a
    trailing inclusive-day aside, and surrounding punctuation; keeps verb +
    object. Capped so a runaway clause can't dominate the embed.
    """
    s = (s or "").strip(" ,.?!'\"")
    s = re.sub(r"^(?:i|we)\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+(?:including the last day)\b.*$", "", s, flags=re.IGNORECASE)
    return s.strip()[:80]


def _verb_object(tail: str) -> tuple[str, str]:
    """Split a clause tail into (main verb, object NP)."""
    toks = [t.strip(",.?!\"'") for t in (tail or "").split() if t.strip(",.?!\"'")]
    i = 0
    while i < len(toks) and toks[i] in {"ever", "already", "actually", "really"}:
        i += 1
    pred = toks[i] if i < len(toks) else ""
    i += 1
    if pred in {"been", "gone", "went"}:
        # "been/gone to X" is structurally a visit to X.
        if i < len(toks) and toks[i] == "to":
            i += 1
        pred = "visit"
    obj = " ".join(t for t in toks[i:] if t not in _DETERMINERS)
    return pred, _clean_np(obj)


def _extract_entities(raw: str) -> tuple[str, ...]:
    """Quoted spans + capitalized (proper-noun-shaped) spans, lowercased.

    Conservative on purpose: entity boosts are additive recall signals,
    never filters, so under-extraction is bounded loss while
    over-extraction adds noise plus per-entity embed cost.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        v = s.strip().lower()
        if not (2 <= len(v) <= 60) or v in seen:
            return
        if v in _WEEKDAYS or _month_num(v):
            return  # date vocabulary, not an entity
        if any(v in prior for prior in out):
            return  # single word already inside a captured span
        seen.add(v)
        out.append(v)

    for m in _QUOTED_RE.finditer(raw):
        add(m[1])
    for m in _CAP_SPAN_RE.finditer(raw):
        span = m[1]
        if span.split()[0] == "I":
            span = " ".join(span.split()[1:])
        if span:
            add(span)
    for m in _CAP_WORD_RE.finditer(raw):
        if m.start() == 0:
            continue  # sentence-initial capitalization is not a name signal
        prev = raw[: m.start()].rstrip()
        if prev.endswith((".", "?", "!")):
            continue
        add(m[1])
    return tuple(out[:4])


def _looks_like_date_phrase(s: str) -> bool:
    s = s.strip()
    if len(s.split()) > 4:
        return False
    return bool(
        _ISO_DATE_RE.search(s)
        or _MDY_RE.search(s)
        or _DMY_RE.search(s)
        or _LAST_PERIOD_RE.search(s)
        or _LAST_WEEKDAY_RE.search(s)
        or _AGO_RE.search(s)
    )


def _between_sub_queries(q_clean: str) -> tuple[str, ...]:
    """The two event clauses of "between A and B", each able to stand as its own
    search; () when either reads as a date or too short."""
    sub_queries: tuple[str, ...] = ()
    mb = re.search(r"\bbetween\s+(.+?)\s+and\s+(.+)$", q_clean)
    if mb:
        a, b = mb[1].strip(" ,"), mb[2].strip(" ,")
        # "between Maria adopting Coco and Shadow": the second event
        # borrows the first's subject and verb ("Maria adopting Shadow").
        if len(b.split()) == 1 and len(a.split()) >= 3 and not _looks_like_date_phrase(a):
            b = " ".join([*a.split()[:-1], b])
        # "between Sam's first and second doctor's appointment": the first
        # event borrows the second's noun phrase.
        a_toks, b_toks = a.split(), b.split()
        if a_toks and b_toks and a_toks[-1] in _ORDINALS and b_toks[0] in _ORDINALS:
            a = " ".join([*a_toks, *b_toks[1:]])
        if (
            len(a.split()) >= 2
            and len(b.split()) >= 2
            and not _looks_like_date_phrase(a)
            and not _looks_like_date_phrase(b)
        ):
            sub_queries = (a, b)
    return sub_queries


def _head_clause_operands(
    q: str, q_clean: str, *, first_person: bool
) -> tuple[tuple[str, ...], bool]:
    """Operands read off the question once its interrogative head is stripped:
    "since/after X1 when/before X2", or "N ago did I E1 [when I E2]"."""
    diff_operands: tuple[str, ...] = ()
    diff_to_obs = False
    # Strip the interrogative head so operand clauses are clean event
    # descriptions ("i launched my website" → "launched my website").
    head = _DATE_DIFF_HEAD_RE.sub("", q_clean)
    head = re.sub(
        r"^\s*(?:passed|elapsed|had\s+passed|have\s+passed|are\s+there|is\s+there)?\s*",
        "",
        head,
    ).strip()
    # "since/after X1 when/before/and X2" → split into two clauses.
    mj = re.search(
        r"\b(?:since|after)\b\s+(.+?)\s+\b(?:when|before|and)\b\s+(.+)$",
        head,
    )
    if mj and first_person:
        a = _clean_clause(mj[1])
        b = _clean_clause(mj[2])
        if a and b:
            diff_operands = (a, b)
    elif _AGO_TAIL_RE.search(q) and first_person:
        ma = re.search(
            r"\bago\s+(?:did|do|have|had|was|were)\s+(?:i|we)\s+(.+)$",
            q_clean,
        )
        if ma:
            tail = ma[1]
            # "...X ago did I E1 when/before I E2" is a two-event diff
            # (E1 vs reference event E2), not event-vs-now: the
            # subordinate clause names the reference date.
            msub = re.search(
                r"^(.+?)\s+\b(?:when|before|after)\b\s+(?:i|we)\s+(.+)$",
                tail,
            )
            if msub:
                a = _clean_clause(msub[1])
                b = _clean_clause(msub[2])
                if a and b:
                    diff_operands = (a, b)
            if not diff_operands:
                # Single event vs observation date.
                clause = _clean_clause(tail)
                if clause:
                    diff_operands = (clause,)
                    diff_to_obs = True
    return diff_operands, diff_to_obs


def _diff_operands(
    q: str, q_clean: str, *, first_person: bool, sub_queries: tuple[str, ...]
) -> tuple[tuple[str, ...], bool]:
    """The dateDiff leg's operand probes and whether the one operand is
    measured against the observation date."""
    # Operand probes for the dateDiff leg, covering three structures:
    #   "how many days AGO did I X"             → one event vs observation
    #   "how many days BETWEEN A and B"         → two events
    #   "...SINCE/AFTER I X1 when/before I X2"  → two event clauses
    # Empty when the clauses don't parse — the compute leg then abstains.
    diff_operands: tuple[str, ...] = ()
    diff_to_obs = False
    # "(for) how long had I been X when/after I Y" → two events (X, Y).
    mdw = re.search(
        r"how\s+long\b.*?\b(?:had|have|did)?\s*(?:i|we)\s+(?:been\s+)?(.+?)"
        r"\s+\b(?:when|after)\b\s+(?:i|we)\s+(.+)$",
        q_clean,
    )
    # "how long after I X did I Y" → events (X, Y).
    mda = re.search(
        r"how\s+long\s+after\s+(?:i|we)\s+(.+?)\s+\b(?:did|do|had|have)\b\s+"
        r"(?:i|we)\s+(.+)$",
        q_clean,
    )
    mtake = _DURATION_TAKE_RE.search(q_clean)
    mbefore = _DURATION_BEFORE_RE.search(q_clean)
    mnp = _DURATION_NP_RE.search(q_clean)
    if sub_queries:
        diff_operands = sub_queries
    elif mtake:
        subj = _clean_clause(mtake[1])
        act = _COMPLETION_HEAD_RE.sub("", _clean_clause(mtake[2]))
        if subj and act:
            diff_operands = (f"{subj} began {act}", f"{subj} finished {act}")
    elif mbefore and not first_person:
        # "how long did X V before Y": the two clauses are the probes as
        # written; the reference clause ("getting married") binds to the
        # dated fact on its own — guessing its subject was measured to
        # produce worse probes than leaving it bare.
        subj_act = _clean_clause(mbefore[1])
        ref = _clean_clause(mbefore[2])
        if subj_act and ref:
            diff_operands = (subj_act, ref)
    elif mnp and not first_person:
        np_ = _clean_clause(mnp[1] or mnp[2])
        if np_:
            diff_operands = (f"{np_} started", f"{np_} ended")
    elif mda and first_person:
        a = _clean_clause(mda[1])
        b = _clean_clause(mda[2])
        if a and b:
            diff_operands = (a, b)
    elif mdw and first_person and not _DATE_DIFF_LINK_RE.search(q):
        a = _clean_clause(mdw[1])
        b = _clean_clause(mdw[2])
        if a and b:
            diff_operands = (a, b)
    else:
        diff_operands, diff_to_obs = _head_clause_operands(q, q_clean, first_person=first_person)
    return diff_operands, diff_to_obs


def _count_intent(
    q: str, q_clean: str, *, first_person: bool, common: dict[str, object]
) -> QueryIntent | None:
    """Counts — occurrence-count vs stated-value-update; None when neither."""
    sv = _STATED_VALUE_RE.search(q_clean) or _STATED_INVENTORY_RE.search(q_clean)
    if sv:
        return QueryIntent(
            query_shape="single_fact",
            op="latest",
            stated_value_update=True,
            category=_clean_np(sv[1]),
            **{**common, "recency_intent": True},  # type: ignore[arg-type]
        )
    mt = _COUNT_TIMES_RE.search(q_clean)
    if mt:
        predicate, object_text = _verb_object(mt[1])
        return QueryIntent(
            query_shape="aggregational",
            op="count",
            predicate=predicate if first_person else "",
            object_text=object_text if first_person else "",
            **common,  # type: ignore[arg-type]
        )
    if _COUNT_RE.search(q):
        mn = _COUNT_NP_RE.search(q_clean) or _COUNT_NP_REL_RE.search(q_clean)
        predicate = ""
        category = ""
        if mn:
            category = _clean_np(mn[1])
            predicate, _obj = _verb_object(mn[2])
        return QueryIntent(
            query_shape="aggregational",
            op="count",
            predicate=predicate if first_person else "",
            category=category if first_person else "",
            **common,  # type: ignore[arg-type]
        )
    return None


def parse_query_intent(query: str, observation_date: str | None = None) -> QueryIntent | None:
    """Classify a query into the enrichment signal shape, or abstain.

    Returns ``None`` (abstain → LLM fallback) for empty, very long, or
    multi-line queries, and for anything no grammar family matched.
    """
    raw = (query or "").strip()
    # Long / multi-line queries carry free-form context the grammar can't
    # decompose; abstain to the LLM.
    if not raw or len(raw) > 180 or "\n" in raw:
        return None
    q = raw.lower()
    q_clean = q.rstrip(" ?!.")

    dates = tuple(resolve_relative_dates(raw, observation_date))
    entities = _extract_entities(raw)
    recency_marker = bool(_RECENCY_RE.search(q))
    first_person = bool(_FIRST_PERSON_RE.search(q))

    common: dict[str, object] = {
        "resolved_dates": dates,
        "entities": entities,
        "recency_intent": recency_marker,
    }

    # 1. Date-span ("how many days between/since/until", "how many days ago",
    #    "how long had I been X when I Y").
    third_person_span = bool(
        _DURATION_TAKE_RE.search(q_clean)
        or _DURATION_BEFORE_RE.search(q_clean)
        or _DURATION_NP_RE.search(q_clean)
    )
    if (
        _AGO_TAIL_RE.search(q)
        or (_DATE_DIFF_HEAD_RE.search(q) and _DATE_DIFF_LINK_RE.search(q))
        or _DURATION_WHEN_RE.search(q)
        or third_person_span
    ):
        predicate = ""
        object_text = ""
        m = re.search(r"\bago\s+(?:did|do|have|had|was|were)\s+(?:i|we)\s+(.*)$", q_clean)
        if m:
            predicate, object_text = _verb_object(m[1])
        sub_queries = _between_sub_queries(q_clean)
        diff_operands, diff_to_obs = _diff_operands(
            q, q_clean, first_person=first_person, sub_queries=sub_queries
        )
        return QueryIntent(
            query_shape="temporal_diff",
            op="date_span",
            predicate=predicate if first_person else "",
            object_text=object_text if first_person else "",
            sub_queries=sub_queries,
            diff_operands=diff_operands,
            diff_anchor_to_observation=diff_to_obs,
            **common,  # type: ignore[arg-type]
        )

    # 2. Sum ("how much did I spend on X", "how much did I raise for X").
    #    The predicate is the query's own money-flow verb so the aggregate
    #    matches the stored occurrences (expand_predicate_class widens the
    #    stem at fetch time).
    if _SUM_RE.search(q):
        m = _SUM_VERB_OBJ_RE.search(q_clean)
        predicate = m[1] if m else ""
        category = _clean_np(m[2]) if m else ""
        return QueryIntent(
            query_shape="aggregational",
            op="sum",
            predicate=predicate if first_person else "",
            category=category if first_person else "",
            **common,  # type: ignore[arg-type]
        )

    # 3. Counts — occurrence-count vs stated-value-update.
    counted = _count_intent(q, q_clean, first_person=first_person, common=common)
    if counted is not None:
        return counted

    # 4. Comparative ordering ("which did I do first, X or Y?").
    has_order_word = bool(_ORDER_FIRST_RE.search(q) or _ORDER_LAST_RE.search(q))
    if " or " in q_clean and has_order_word and first_person:
        chrono = "asc" if _ORDER_FIRST_RE.search(q) else "desc"
        head = re.sub(r"^(?:which|what|who|did|when|do|does)\b[\s,]*", "", q_clean)
        parts = [p.strip(" ,") for p in re.split(r"\bor\b", head) if p.strip(" ,")]
        sub_queries = ()
        if len(parts) == 2:
            a = parts[0]
            b = re.sub(r"\b(?:first|last|earlier|later)\b\s*$", "", parts[1]).strip(" ,")
            if a and b:
                sub_queries = (a, b)
        return QueryIntent(
            query_shape="comparative",
            chronological_intent=chrono,
            sub_queries=sub_queries,
            **common,  # type: ignore[arg-type]
        )
    if first_person and re.search(r"\b(?:which|what|who)\b.*\bfirst\b", q_clean):
        return QueryIntent(
            query_shape="comparative",
            chronological_intent="asc",
            **common,  # type: ignore[arg-type]
        )

    # 5. When-did lookups ("when did I first/last ...", "what year ...").
    if _WHEN_RE.search(q_clean):
        chrono = ""
        if _ORDER_FIRST_RE.search(q):
            chrono = "asc"
        elif _ORDER_LAST_RE.search(q):
            chrono = "desc"
        return QueryIntent(
            query_shape="single_fact",
            chronological_intent=chrono,
            **common,  # type: ignore[arg-type]
        )

    # 6. Latest/current value ("what X do I use now?", "current ...").
    if recency_marker:
        pref = ""
        mp = _PREFERENCE_RE.search(q_clean)
        if mp:
            pref = _clean_np(mp[1])
        return QueryIntent(
            query_shape="single_fact",
            op="latest",
            preference_attribute=pref,
            **{**common, "recency_intent": True},  # type: ignore[arg-type]
        )

    # 7. Dated lookup — relative/absolute date with no other structure.
    if dates:
        return QueryIntent(
            query_shape="single_fact",
            **common,  # type: ignore[arg-type]
        )

    return None
