"""Tests for server/memory/domain/query_intent.py — deterministic query-intent parser."""

from __future__ import annotations

import pytest

from server.memory.domain.query_intent import (
    QueryIntent,
    parse_query_intent,
    resolve_relative_dates,
)

# Fixed anchor: 2023-07-15 is a Saturday.
OBS = "2023-07-15"

# (query, expected field subset). None = abstain.
CASES: list[tuple[str, dict | None]] = [
    (
        "how many model kits have I worked on?",
        {
            "query_shape": "aggregational",
            "op": "count",
            "category": "model kits",
            "predicate": "worked",
            "stated_value_update": False,
        },
    ),
    (
        "how many times did I visit the dentist?",
        {
            "query_shape": "aggregational",
            "op": "count",
            "predicate": "visit",
            "object_text": "dentist",
            "stated_value_update": False,
        },
    ),
    (
        "how many times have I been to the gym?",
        {"op": "count", "predicate": "visit", "object_text": "gym"},
    ),
    (
        "how many concerts did I attend last year?",
        {"op": "count", "category": "concerts", "predicate": "attend"},
    ),
    (
        "how many model kits I have built?",
        {
            "query_shape": "aggregational",
            "op": "count",
            "category": "model kits",
            "predicate": "built",
        },
    ),
    (
        "how many books I read this year?",
        {"op": "count", "category": "books", "predicate": "read"},
    ),
    (
        "how many distinct cities have I visited?",
        {"op": "count", "category": "cities", "predicate": "visited"},
    ),
    (
        "how many different restaurants have I tried?",
        {"op": "count", "category": "restaurants", "predicate": "tried"},
    ),
    (
        "how many books do I have now?",
        {
            "stated_value_update": True,
            "recency_intent": True,
            "op": "latest",
            "query_shape": "single_fact",
        },
    ),
    (
        "how many magazines do I currently own?",
        {"stated_value_update": True, "recency_intent": True},
    ),
    (
        "how many plants are in my apartment?",
        {"stated_value_update": True, "category": "plants"},
    ),
    (
        "how much did I spend on bike gear?",
        {
            "query_shape": "aggregational",
            "op": "sum",
            "predicate": "spend",
            "category": "bike gear",
        },
    ),
    (
        "what is the total amount I spent on collectibles?",
        {"op": "sum", "category": "collectibles"},
    ),
    (
        "how much money have I paid for streaming subscriptions?",
        {"op": "sum", "category": "streaming subscriptions"},
    ),
    (
        "how much money did I raise for charity in total?",
        {"query_shape": "aggregational", "op": "sum", "predicate": "raise", "category": "charity"},
    ),
    (
        "how much did I earn from freelancing in total?",
        {"op": "sum", "predicate": "earn", "category": "freelancing"},
    ),
    (
        "what is the total amount I saved for my emergency fund?",
        {"op": "sum"},
    ),
    (
        "how much have I donated to the food bank this year?",
        {"op": "sum", "predicate": "donated", "category": "food bank"},
    ),
    (
        "how much money do I have in my savings now?",
        {"query_shape": "single_fact", "op": "latest", "recency_intent": True},
    ),
    (
        "how many days passed between my first pottery class and the craft fair?",
        {
            "query_shape": "temporal_diff",
            "op": "date_span",
            "sub_queries": ["my first pottery class", "the craft fair"],
        },
    ),
    (
        "how many days ago did I buy the smoker?",
        {
            "query_shape": "temporal_diff",
            "op": "date_span",
            "predicate": "buy",
            "object_text": "smoker",
        },
    ),
    (
        "how many weeks ago did I start the course?",
        {"op": "date_span", "predicate": "start"},
    ),
    (
        "how many days until my lease ends?",
        {"query_shape": "temporal_diff", "op": "date_span"},
    ),
    (
        "which did I visit first, the museum or the aquarium?",
        {"query_shape": "comparative", "chronological_intent": "asc"},
    ),
    (
        "did I adopt the cat or buy the couch first?",
        {
            "query_shape": "comparative",
            "chronological_intent": "asc",
            "sub_queries": ["i adopt the cat", "buy the couch"],
        },
    ),
    (
        "what laptop do I use now?",
        {
            "query_shape": "single_fact",
            "op": "latest",
            "recency_intent": True,
            "preference_attribute": "laptop",
        },
    ),
    (
        "what is my current phone plan?",
        {"recency_intent": True, "op": "latest"},
    ),
    (
        "when did I first try rock climbing?",
        {"query_shape": "single_fact", "chronological_intent": "asc"},
    ),
    (
        "when did I last visit the dentist?",
        {"query_shape": "single_fact", "chronological_intent": "desc"},
    ),
    (
        "what year did we move to the coast?",
        {"query_shape": "single_fact"},
    ),
    (
        "what did I do last weekend?",
        {"query_shape": "single_fact", "resolved_dates": ["2023-07-08"]},
    ),
    (
        "what restaurant did I go to yesterday?",
        {"resolved_dates": ["2023-07-14"]},
    ),
    (
        "what concerts did I attend last Tuesday?",
        {"resolved_dates": ["2023-07-11"]},
    ),
    (
        "what happened on March 15, 2023?",
        {"resolved_dates": ["2023-03-15"]},
    ),
    (
        "what did I do two weeks ago?",
        {"resolved_dates": ["2023-07-01"]},
    ),
    (
        # Yearless month-day resolves to the most recent past occurrence.
        "what gift did I get for Pam on January 5?",
        {"resolved_dates": ["2023-01-05"], "entities": ["pam"]},
    ),
    ("tell me about my preferences", None),
    ("compare my project assignments across the two teams", None),
    ("what should I cook for dinner?", None),
    ("", None),
    ("x" * 181, None),
    ("how many kits?\nalso, summarize my week", None),  # multi-line
]


@pytest.mark.parametrize("query,expected", CASES, ids=[c[0][:48] or "<empty>" for c in CASES])
def test_parse_query_intent_table(query: str, expected: dict | None) -> None:
    out = parse_query_intent(query, OBS)
    if expected is None:
        assert out is None
        return
    assert out is not None, f"parser abstained on {query!r}"
    for field_name, want in expected.items():
        got = getattr(out, field_name)
        if isinstance(got, tuple):
            got = list(got)
        assert got == want, f"{field_name}: {got!r} != {want!r} for {query!r}"


def test_enrichment_signal_matches_llm_tuple_shape() -> None:
    out = parse_query_intent("how many books did I buy last month?", OBS)
    assert out is not None
    (dates, chrono, recency, pref, entities, subs, shape) = out.enrichment_signal()
    assert dates == ["2023-06-15"]
    assert chrono == ""
    assert recency is False
    assert pref == ""
    assert isinstance(entities, list)
    assert isinstance(subs, list)
    assert shape == "aggregational"


def test_derived_intent_occurrence_count_routes_to_count() -> None:
    out = parse_query_intent("how many times did I visit the dentist?", OBS)
    assert out is not None
    intent = out.derived_intent()
    assert intent == {
        "op": "count",
        "subject": "user",
        "predicate": "visit",
        "category": "",
        "object": "dentist",
    }


def test_derived_intent_stated_value_update_never_counts() -> None:
    """'how many X do I have now' is a latest-stated-value lookup, not a COUNT."""
    out = parse_query_intent("how many books do I have now?", OBS)
    assert out is not None
    assert out.stated_value_update is True
    assert out.derived_intent() is None
    assert out.recency_intent is True


def test_derived_intent_requires_at_least_one_operand_dimension() -> None:
    intent = QueryIntent(query_shape="aggregational", op="count")
    assert intent.derived_intent() is None


def test_relative_clause_count_routes_to_count() -> None:
    """'how many X I have built' (no subject-aux inversion) routes to COUNT."""
    out = parse_query_intent("how many model kits I have built?", OBS)
    assert out is not None
    intent = out.derived_intent()
    assert intent is not None
    assert intent["op"] == "count"
    assert intent["category"] == "model kits"
    assert intent["predicate"] == "built"


def test_distinct_qualifier_is_stripped_from_count_category() -> None:
    """A count quantifier ('distinct'/'different'/'unique') is not part of the category."""
    out = parse_query_intent("how many distinct cities have I visited?", OBS)
    assert out is not None
    assert out.category == "cities"


def test_distinct_qualifier_does_not_break_stated_value_guard() -> None:
    """'how many distinct X do I have now' is still a latest-stated-value lookup."""
    out = parse_query_intent("how many distinct coffees do I have now?", OBS)
    assert out is not None
    assert out.stated_value_update is True
    assert out.derived_intent() is None


def test_derived_intent_sum_carries_category() -> None:
    out = parse_query_intent("how much did I spend on bike gear?", OBS)
    assert out is not None
    intent = out.derived_intent()
    assert intent is not None
    assert intent["op"] == "sum"
    assert intent["category"] == "bike gear"


def test_derived_intent_money_inflow_sum_routes_with_query_verb() -> None:
    """Money-inflow SUM keys on the query's own verb, not a hardcoded 'spend'."""
    out = parse_query_intent("how much money did I raise for charity in total?", OBS)
    assert out is not None
    assert out.query_shape == "aggregational"
    intent = out.derived_intent()
    assert intent is not None
    assert intent["op"] == "sum"
    assert intent["predicate"] == "raise"
    assert intent["category"] == "charity"


def test_stated_balance_lookup_is_not_a_sum() -> None:
    """'how much money do I have now' is a balance lookup; no money-flow verb → not SUM."""
    out = parse_query_intent("how much money do I have in my savings now?", OBS)
    assert out is not None
    assert out.op != "sum"
    assert out.derived_intent() is None


def test_money_inflow_predicate_class_reaches_stored_stems() -> None:
    """Inflow verbs collapse into one class so a 'raised' SUM matches earn/collect stems."""
    from server.memory.domain.search_parsing import expand_predicate_class
    from server.memory.domain.search_text import canonicalize_verb

    cls = expand_predicate_class(canonicalize_verb("raise"))
    assert canonicalize_verb("earn") in cls
    assert canonicalize_verb("collect") in cls
    assert canonicalize_verb("spend") not in cls


@pytest.mark.parametrize(
    "query,verb",
    [
        ("how much did I donate to charity in total?", "donate"),
        ("how much did I give to charity overall?", "give"),
        ("how much have I contributed to charity?", "contributed"),
        ("how much did I pledge to the campaign in total?", "pledge"),
    ],
)
def test_charity_giving_verbs_route_to_sum(query: str, verb: str) -> None:
    """Charitable-giving verbs (donate/give/contribute/pledge) route to SUM."""
    out = parse_query_intent(query, OBS)
    assert out is not None
    intent = out.derived_intent()
    assert intent is not None
    assert intent["op"] == "sum"
    assert intent["predicate"] == verb


def test_giving_verbs_share_one_inflow_predicate_class() -> None:
    """donate/give/contribute/pledge canonicalize into the same inflow class as raise/earn."""
    from server.memory.domain.search_parsing import expand_predicate_class
    from server.memory.domain.search_text import canonicalize_verb

    cls = expand_predicate_class(canonicalize_verb("donate"))
    for v in ("raise", "give", "contribute", "pledge", "earn"):
        assert canonicalize_verb(v) in cls
    assert canonicalize_verb("spend") not in cls


def test_gathering_count_routes_with_attend_predicate() -> None:
    """'how many dinner parties have I attended' routes to COUNT keyed on 'attend'."""
    out = parse_query_intent("how many dinner parties have I attended in the past month?", OBS)
    assert out is not None
    intent = out.derived_intent()
    assert intent is not None
    assert intent["op"] == "count"
    assert intent["predicate"] == "attended"
    assert "dinner" in intent["category"]


def test_gathering_verbs_reach_the_visitation_class() -> None:
    """host/throw join attend/visit/go in one class; acquisition verbs do not."""
    from server.memory.domain.search_parsing import expand_predicate_class
    from server.memory.domain.search_text import canonicalize_verb

    cls = expand_predicate_class(canonicalize_verb("attend"))
    for v in ("visit", "go", "host", "throw"):
        assert canonicalize_verb(v) in cls
    assert canonicalize_verb("buy") not in cls


def test_pending_intention_count_carries_no_stored_predicate_class() -> None:
    """Pending intentions ('need to pick up/return') map to no occurrence class."""
    from server.memory.domain.search_parsing import expand_predicate_class
    from server.memory.domain.search_text import canonicalize_verb

    out = parse_query_intent("how many items of clothing do I need to pick up or return?", OBS)
    assert out is not None
    intent = out.derived_intent()
    if intent and intent.get("predicate"):
        assert expand_predicate_class(canonicalize_verb(intent["predicate"])) == set()


def test_third_person_count_does_not_fabricate_user_operands() -> None:
    out = parse_query_intent("how many moons does Jupiter have?", OBS)
    if out is not None:
        assert out.derived_intent() is None or out.predicate == ""


# (query, expected spec | None). None = date-arith leg abstains.
TEMPORAL_DIFF_CASES: list[tuple[str, dict | None]] = [
    # Two-event "between A and B": two operands, vs each other.
    (
        "how many days passed between the day I cancelled my subscription "
        "and the day I reordered groceries?",
        {
            "operands": [
                "the day i cancelled my subscription",
                "the day i reordered groceries",
            ],
            "to_observation": False,
        },
    ),
    # Single event "X ago": one operand, vs the observation date.
    (
        "how many days ago did I attend a networking event?",
        {"operands": ["attend a networking event"], "to_observation": True},
    ),
    # "X ago ... when I Y" is a two-event diff: the clause names the reference event.
    (
        "how many days ago did I launch my website when I signed a contract?",
        {
            "operands": ["launch my website", "signed a contract"],
            "to_observation": False,
        },
    ),
    # "since I X1 when I X2": two-event clause split.
    (
        "how many weeks had passed since I recovered from the flu when I went on my 10th jog?",
        {
            "operands": ["recovered from the flu", "went on my 10th jog"],
            "to_observation": False,
        },
    ),
    # Duration head "(for) how long had I been X when I Y" — two events.
    (
        "for how long had I been using the new binoculars when I noticed "
        "the goldfinches returning?",
        {
            "operands": [
                "using the new binoculars",
                "noticed the goldfinches returning",
            ],
            "to_observation": False,
        },
    ),
    # "how long after I X did I Y" — two events.
    (
        "how long after I bought my tennis racket did I receive the strings?",
        {
            "operands": ["bought my tennis racket", "receive the strings"],
            "to_observation": False,
        },
    ),
    # Non-temporal / non-diff shapes abstain (no operands → no compute).
    ("what camera do I use now?", None),
    ("how many books have I read this year?", None),
    ("how long is the Great Wall of China?", None),
]


@pytest.mark.parametrize(
    "query,expected",
    TEMPORAL_DIFF_CASES,
    ids=[c[0][:48] for c in TEMPORAL_DIFF_CASES],
)
def test_temporal_diff_spec(query: str, expected: dict | None) -> None:
    out = parse_query_intent(query, OBS)
    spec = out.temporal_diff_spec() if out is not None else None
    assert spec == expected, f"{spec!r} != {expected!r} for {query!r}"


def test_temporal_diff_spec_takes_third_person_event_clauses() -> None:
    # The compute leg does not need a first-person question;
    # two named events are two probes, and the leg still fail-opens when a
    # probe binds to nothing dated.
    out = parse_query_intent("how many days passed between the eclipse and the solstice?", OBS)
    spec = out.temporal_diff_spec() if out is not None else None
    assert spec is not None and list(spec["operands"]) == ["the eclipse", "the solstice"]
    # A span head with no event clauses still yields no spec.
    bare = parse_query_intent("how many days passed?", OBS)
    assert bare is None or bare.temporal_diff_spec() is None


DATE_CASES: list[tuple[str, list[str]]] = [
    ("what did I do today?", ["2023-07-15"]),
    ("what did I do yesterday?", ["2023-07-14"]),
    ("the day before yesterday I went hiking", ["2023-07-13"]),
    ("dinner last Friday", ["2023-07-14"]),
    ("dinner this past Friday", ["2023-07-14"]),
    ("last Saturday's match", ["2023-07-08"]),  # strictly before the anchor
    ("what happened last week?", ["2023-07-08"]),
    ("what happened last month?", ["2023-06-15"]),
    ("3 days ago", ["2023-07-12"]),
    ("a week ago", ["2023-07-08"]),
    ("two months ago", ["2023-05-16"]),
    ("on 2023-05-24 I bought a vase", ["2023-05-24"]),
    ("March 15, 2023", ["2023-03-15"]),
    ("15 March 2023", ["2023-03-15"]),
    ("in March 2024", ["2024-03-01"]),
    # Yearless month-day after the anchor rolls back a year.
    ("on December 25", ["2022-12-25"]),
    ("no dates here", []),
]


@pytest.mark.parametrize("query,expected", DATE_CASES, ids=[c[0][:40] for c in DATE_CASES])
def test_resolve_relative_dates(query: str, expected: list[str]) -> None:
    assert resolve_relative_dates(query, OBS) == expected


def test_relative_dates_need_an_anchor() -> None:
    # Without an anchor only absolute forms resolve.
    assert resolve_relative_dates("yesterday", None) == []
    assert resolve_relative_dates("on March 15, 2023", None) == ["2023-03-15"]
    assert resolve_relative_dates("on March 15", None) == []


def test_resolved_dates_capped_and_deduped() -> None:
    q = "2023-01-01 2023-01-02 2023-01-03 2023-01-04 2023-01-05 2023-01-01"
    out = resolve_relative_dates(q, OBS)
    assert len(out) == 4
    assert len(set(out)) == 4
