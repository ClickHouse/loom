"""The memory.ask synthesis router is a pure function — pin its rules.

Every rule has a stable `reason` token because the wire output and the
replay bench group outcomes by it; renaming one is a contract change.
"""

from __future__ import annotations

import pytest

from server.memory.domain import synth_policy as sp


def _easy(**over) -> sp.SynthFeatures:
    base = dict(
        query_chars=30,
        n_memories=3,
        retrieval_confidence=0.7,
        retrieval_risk="medium",
        route_agreement=0.8,
        top_similarity=0.78,
        similarity_margin=0.1,
        hedged=False,
        as_of=False,
        budget_ratio=0.1,
        query="what is my favorite color",
    )
    base.update(over)
    return sp.SynthFeatures(**base)


def test_easy_question_goes_cheap() -> None:
    d = sp.decide(_easy())
    assert d.cheap and d.reason == "easy_question"


def test_defaults_are_conservative() -> None:
    # A caller with no signals at all must land on the strong tier.
    d = sp.decide(sp.SynthFeatures())
    assert d.tier == sp.STRONG


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"hedged": True}, "identifier_ungrounded"),
        ({"as_of": True}, "as_of_anchor"),
        ({"query": "which PRs did we review last week"}, "breadth_query"),
        ({"query": "summarize everything about the outage"}, "breadth_query"),
        ({"retrieval_risk": "high"}, "retrieval_risk_high"),
        ({"retrieval_risk": "abstain"}, "retrieval_risk_abstain"),
        ({"retrieval_risk": ""}, "retrieval_risk_unknown"),
        ({"retrieval_confidence": 0.5}, "low_confidence"),
        ({"top_similarity": 0.6}, "weak_top_hit"),
        ({"route_agreement": 0.2}, "route_disagreement"),
        ({"query_chars": 400}, "long_query"),
    ],
)
def test_hard_signals_route_strong(over, reason) -> None:
    d = sp.decide(_easy(**over))
    assert d.tier == sp.STRONG
    assert d.reason == reason


def test_budget_pressure_wins_over_hardness() -> None:
    d = sp.decide(_easy(hedged=True, retrieval_risk="high", budget_ratio=0.8))
    assert d.cheap and d.reason == "budget_pressure"


def test_budget_rule_can_be_disabled() -> None:
    d = sp.decide(_easy(hedged=True, budget_ratio=0.95), budget_cheap_ratio=0)
    assert d.tier == sp.STRONG and d.reason == "identifier_ungrounded"


def test_medium_risk_alone_does_not_block_cheap() -> None:
    # Most real questions band as "medium"; the continuous
    # confidence and top-hit similarity are the gates, not the band.
    assert sp.decide(_easy(retrieval_risk="medium")).cheap


def test_thresholds_are_tunable() -> None:
    assert sp.decide(_easy(retrieval_confidence=0.5), min_cheap_confidence=0.45).cheap
    assert sp.decide(_easy(top_similarity=0.6), min_cheap_top_similarity=0.55).cheap
    assert sp.decide(_easy(route_agreement=0.3), min_cheap_agreement=0.2).cheap
    # Pool-width gate is off by default (the pool is the configured window).
    assert sp.decide(_easy(n_memories=15)).cheap
    d = sp.decide(_easy(n_memories=15), max_cheap_memories=6)
    assert d.tier == sp.STRONG and d.reason == "wide_pool"


@pytest.mark.parametrize(
    "answer, cited, expected",
    [
        ("", ["m1"], "cheap_empty"),
        ("   ", ["m1"], "cheap_empty"),
        ("Blue.", [], "cheap_uncited"),
        ("I have no memory specifically about PR 123 [m1].", ["m1"], "cheap_hedged"),
        ("I don't know from these memories [m1].", ["m1"], "cheap_hedged"),
        ("Your favorite color is blue [m1].", ["m1"], ""),
    ],
)
def test_cheap_answer_verifier(answer, cited, expected) -> None:
    assert sp.cheap_answer_failed(answer, cited) == expected


@pytest.mark.parametrize(
    "query, breadth",
    [
        ("how do I install the plugin", False),
        ("compare the two approaches", True),
        ("which PRs did we review last week", True),
        ("how many PRs has the user merged", True),
        ("list all the flaky tests", True),
        ("what are all the open issues", True),
        # One entity, not a set: 25 of 60 sampled real questions take this form.
        ("what do we know about PR 117040", False),
        # "all" inside a noun phrase is not enumeration.
        ("what did the user query CIDB for regarding all new failures", False),
        ("what command gets the task_list queue", False),
    ],
)
def test_breadth_detector(query, breadth) -> None:
    assert sp.is_breadth_query(query) is breadth


# --- identifier questions are not easy ------------------------


def _easy(**over):
    from server.memory.domain.synth_policy import SynthFeatures

    base = dict(
        query_chars=60,
        n_memories=12,
        retrieval_confidence=0.8,
        retrieval_risk="medium",
        route_agreement=0.9,
        top_similarity=0.85,
        similarity_margin=0.2,
        query="What do we know about PR 107585?",
    )
    base.update(over)
    return SynthFeatures(**base)


def test_identifier_question_routes_strong_even_when_signals_look_easy() -> None:
    from server.memory.domain.synth_policy import STRONG, decide

    d = decide(_easy(has_identifier=True))
    assert d.tier == STRONG and d.reason == "identifier_question"


def test_no_identifier_keeps_the_easy_question_route() -> None:
    from server.memory.domain.synth_policy import CHEAP, decide

    assert decide(_easy(has_identifier=False)).tier == CHEAP


# ------------------------------------------------- verdict prior


def _uncertain(reason: str) -> sp.SynthFeatures:
    over = {
        "weak_top_hit": {"top_similarity": 0.6},
        "low_confidence": {"retrieval_confidence": 0.5},
        "retrieval_risk_high": {"retrieval_risk": "high"},
        "route_disagreement": {"route_agreement": 0.2},
    }[reason]
    return _easy(**over)


def test_no_prior_is_the_rule_only_router() -> None:
    d = sp.decide(_uncertain("weak_top_hit"))
    assert d.tier == sp.STRONG and d.reason == "weak_top_hit"
    d2 = sp.decide(_uncertain("weak_top_hit"), route_prior=sp.MEASURED_ROUTE_PRIOR)
    assert d2 == d, "a prior with max_cheap_prior 0 changes nothing"


def test_prior_hands_a_low_contradiction_route_to_the_cheap_tier() -> None:
    d = sp.decide(
        _uncertain("weak_top_hit"), route_prior={"weak_top_hit": 0.08}, max_cheap_prior=0.10
    )
    assert d.cheap and d.reason == "weak_top_hit:prior_cheap"


def test_prior_keeps_a_route_the_verdict_often_contradicts_strong() -> None:
    for reason in ("weak_top_hit", "low_confidence", "retrieval_risk_high"):
        d = sp.decide(_uncertain(reason), route_prior=sp.MEASURED_ROUTE_PRIOR, max_cheap_prior=0.10)
        assert d.tier == sp.STRONG and d.reason == reason, reason


def test_measured_prior_moves_only_weak_top_hit_at_two_tenths() -> None:
    moved = {
        r: sp.decide(_uncertain(r), route_prior=sp.MEASURED_ROUTE_PRIOR, max_cheap_prior=0.20)
        for r in ("weak_top_hit", "low_confidence", "retrieval_risk_high")
    }
    assert (
        moved["weak_top_hit"].cheap and moved["weak_top_hit"].reason == "weak_top_hit:prior_cheap"
    )
    assert moved["low_confidence"].tier == sp.STRONG
    assert moved["retrieval_risk_high"].tier == sp.STRONG


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"hedged": True}, "identifier_ungrounded"),
        ({"has_identifier": True}, "identifier_question"),
        ({"as_of": True}, "as_of_anchor"),
        ({"query": "which PRs did we review last week"}, "breadth_query"),
        ({"retrieval_risk": "abstain"}, "retrieval_risk_abstain"),
    ],
)
def test_hard_stops_never_move_on_the_prior(over, reason) -> None:
    prior = {reason: 0.0}  # even a route the verdict never contradicts
    d = sp.decide(_easy(**over), route_prior=prior, max_cheap_prior=0.5)
    assert d.tier == sp.STRONG and d.reason == reason


def test_prior_ignores_routes_it_does_not_know_and_an_easy_question_stays_cheap() -> None:
    d = sp.decide(
        _uncertain("route_disagreement"), route_prior={"weak_top_hit": 0.0}, max_cheap_prior=0.5
    )
    assert d.tier == sp.STRONG and d.reason == "route_disagreement"
    e = sp.decide(_easy(), route_prior=sp.MEASURED_ROUTE_PRIOR, max_cheap_prior=0.5)
    assert e.cheap and e.reason == "easy_question"


def test_parse_route_prior_grammar() -> None:
    assert sp.parse_route_prior("") is None and sp.parse_route_prior("off") is None
    assert sp.parse_route_prior(None) is None
    assert sp.parse_route_prior("measured") is sp.MEASURED_ROUTE_PRIOR
    assert sp.parse_route_prior('{"weak_top_hit": "0.19", "x": "nan?"}') == {"weak_top_hit": 0.19}
    assert sp.parse_route_prior("not json") is None and sp.parse_route_prior("[1,2]") is None
    assert sp.parse_route_prior({"a": 1}) == {"a": 1.0}


def test_measured_prior_is_ordered_the_way_the_ledger_read() -> None:
    p = sp.MEASURED_ROUTE_PRIOR
    assert p["retrieval_risk_high"] > p["low_confidence"] > p["weak_top_hit"] > p["breadth_query"]
    assert p["easy_question"] < 0.10 and p["single_tier"] < 0.10
    assert sp.PRIOR_HARD_STOPS >= {"identifier_question", "breadth_query", "rich_evidence"}


# ---- the mid tier -------------------------------------------


def _uncertain_route(reason: str) -> sp.SynthFeatures:
    """Features whose rule ladder lands on the strong tier for `reason`."""
    if reason == "weak_top_hit":
        return sp.SynthFeatures(
            retrieval_confidence=0.7,
            retrieval_risk="medium",
            top_similarity=0.6,
            route_agreement=0.9,
            query="what colour is it",
        )
    if reason == "low_confidence":
        return sp.SynthFeatures(
            retrieval_confidence=0.4,
            retrieval_risk="medium",
            top_similarity=0.8,
            route_agreement=0.9,
            query="what colour is it",
        )
    if reason == "retrieval_risk_high":
        return sp.SynthFeatures(
            retrieval_confidence=0.7,
            retrieval_risk="high",
            top_similarity=0.8,
            route_agreement=0.9,
            query="what colour is it",
        )
    raise AssertionError(reason)


def test_no_mid_routes_is_the_two_tier_router() -> None:
    for reason in ("weak_top_hit", "low_confidence", "retrieval_risk_high"):
        d = sp.decide(_uncertain_route(reason))
        assert d.tier == sp.STRONG and d.reason == reason and not d.mid


@pytest.mark.parametrize("reason", ["weak_top_hit", "low_confidence", "retrieval_risk_high"])
def test_uncertain_routes_go_to_the_mid_tier(reason: str) -> None:
    from server.memory.domain.synth_policy import MID, MID_ROUTES_UNCERTAIN

    d = sp.decide(_uncertain_route(reason), mid_routes=MID_ROUTES_UNCERTAIN)
    assert d.tier == MID and d.mid and not d.cheap
    assert d.reason == f"{reason}:mid"


def test_a_custom_mid_route_list_moves_only_what_it_names() -> None:
    from server.memory.domain.synth_policy import MID, parse_mid_routes

    routes = parse_mid_routes("weak_top_hit")
    assert sp.decide(_uncertain_route("weak_top_hit"), mid_routes=routes).tier == MID
    assert sp.decide(_uncertain_route("low_confidence"), mid_routes=routes).tier == sp.STRONG


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"hedged": True}, "identifier_ungrounded"),
        ({"has_identifier": True}, "identifier_question"),
        ({"as_of": True}, "as_of_anchor"),
        ({"query": "which PRs did we review"}, "breadth_query"),
        ({"retrieval_risk": "abstain"}, "retrieval_risk_abstain"),
        ({"evidence_tokens": 9000}, "rich_evidence"),
    ],
)
def test_hard_stops_never_move_to_the_mid_tier(over: dict, reason: str) -> None:
    from server.memory.domain.synth_policy import PRIOR_HARD_STOPS, parse_mid_routes

    # Even a list that names the hard stop by hand is filtered.
    routes = parse_mid_routes(",".join(sorted(PRIOR_HARD_STOPS)) + ",weak_top_hit")
    assert routes == frozenset({"weak_top_hit"})
    f = sp.SynthFeatures(**{**_easy().__dict__, **over})
    d = sp.decide(f, max_cheap_evidence_tokens=2500, mid_routes=frozenset(PRIOR_HARD_STOPS))
    assert d.tier == sp.STRONG and d.reason == reason


def test_easy_questions_stay_cheap_and_the_prior_wins_before_the_mid_tier() -> None:
    from server.memory.domain.synth_policy import MEASURED_ROUTE_PRIOR, MID_ROUTES_UNCERTAIN

    assert sp.decide(_easy(), mid_routes=MID_ROUTES_UNCERTAIN).tier == sp.CHEAP
    # The verdict prior moves weak_top_hit cheap at 0.20; the mid tier is the
    # fallback for what the prior leaves strong, not a replacement for it.
    d = sp.decide(
        _uncertain_route("weak_top_hit"),
        route_prior=MEASURED_ROUTE_PRIOR,
        max_cheap_prior=0.20,
        mid_routes=MID_ROUTES_UNCERTAIN,
    )
    assert d.tier == sp.CHEAP and d.reason == "weak_top_hit:prior_cheap"
    d2 = sp.decide(
        _uncertain_route("low_confidence"),
        route_prior=MEASURED_ROUTE_PRIOR,
        max_cheap_prior=0.20,
        mid_routes=MID_ROUTES_UNCERTAIN,
    )
    assert d2.tier == "mid" and d2.reason == "low_confidence:mid"


def test_parse_mid_routes_grammar() -> None:
    from server.memory.domain.synth_policy import MID_ROUTES_UNCERTAIN, parse_mid_routes

    assert parse_mid_routes("") is None and parse_mid_routes("off") is None
    assert parse_mid_routes(None) is None
    assert parse_mid_routes("uncertain") == MID_ROUTES_UNCERTAIN
    assert parse_mid_routes("UNCERTAIN") == MID_ROUTES_UNCERTAIN
    assert parse_mid_routes(" weak_top_hit , low_confidence ") == frozenset(
        {"weak_top_hit", "low_confidence"}
    )
    assert parse_mid_routes("breadth_query") is None, "a list of hard stops alone is no list"


def test_the_verifier_names_the_tier_that_failed() -> None:
    assert sp.cheap_answer_failed("", [], tier="mid") == "mid_empty"
    assert sp.cheap_answer_failed("Blue.", [], tier="mid") == "mid_uncited"
    assert sp.cheap_answer_failed("I don't know [m1].", ["m1"], tier="mid") == "mid_hedged"
    assert sp.cheap_answer_failed("Blue [m1].", ["m1"], tier="mid") == ""
    assert sp.cheap_answer_failed("Blue.", []) == "cheap_uncited", "the default is the cheap tier"


@pytest.mark.parametrize(
    ("query", "composed"),
    [
        (
            "What is the country of citizenship of the spouse of the author of Our Mutual Friend?",
            True,
        ),
        (
            "What is the capital city of the country where the sport associated with X has its origin?",
            True,
        ),
        (
            "From which country does the sport, with which Carlos Gómez is associated, come from?",
            True,
        ),
        ("What is the job title of the chairperson of the political organization Fatah?", True),
        ("What is the name of the restaurant I visited last week?", False),
        ("In which city did I meet Sarah?", False),
        ("Which sport is goaltender associated with?", False),
        ("What is the capital of Italy?", False),
    ],
)
def test_composed_detector(query, composed) -> None:
    assert sp.is_composed_query(query) is composed


def test_a_composed_question_goes_strong_even_when_retrieval_looks_easy() -> None:
    q = "What is the capital city of the country where the sport associated with X originated?"
    d = sp.decide(_easy(query=q, query_chars=80))
    assert (d.tier, d.reason) == (sp.STRONG, "composed_question")


def test_the_route_prior_never_moves_a_composed_question_cheap() -> None:
    q = "What is the job title of the chairperson of the political organization Fatah?"
    d = sp.decide(
        _easy(query=q, query_chars=70),
        route_prior={"composed_question": 0.0},
        max_cheap_prior=0.5,
    )
    assert d.tier == sp.STRONG
