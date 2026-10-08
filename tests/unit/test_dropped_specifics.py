"""The extractor keeps a start/finish clause and a stated cost it would otherwise drop."""

from __future__ import annotations

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.enrich import _recover_specifics
from server.memory.domain.extraction_specifics import stated_costs, stated_events

BIKE = (
    "I've had good experiences with the local bike shop downtown where I bought my bike "
    "helmet for $120. They did a great job with the tune-up last time."
)
BOOKS = (
    "I'm interested in learning more about the themes of identity, culture, and power in "
    '"The Fifth Season" by N.K. Jemisin. I\'ve been thinking a lot about how these themes relate '
    'to my own experiences as a woman, and I started listening to "The Power" by Naomi Alderman '
    "today. Any thoughts?"
)


def test_a_stated_cost_is_found_with_its_object_and_amount() -> None:
    costs = stated_costs(BIKE)
    assert len(costs) == 1
    _, verb, obj, amt = costs[0]
    assert (verb, amt) == ("bought", "$120")
    assert obj.startswith("my bike helmet for")


def test_questions_and_amountless_purchases_are_skipped() -> None:
    assert stated_costs("Should I buy the $120 helmet?") == []
    assert stated_costs("I bought a helmet at the shop downtown.") == []


def test_a_start_clause_is_found_without_its_time_word() -> None:
    events = stated_events(BOOKS)
    assert len(events) == 1
    _, verb, obj = events[0]
    assert verb == "started"
    assert obj == 'listening to "The Power" by Naomi Alderman'


def test_pipeline_adds_the_cost_row_and_the_start_event() -> None:
    msgs = [{"role": "user", "content": BIKE}, {"role": "user", "content": BOOKS}]
    facts = [
        ExtractedFact(
            memory_key="user_bike_helmet",
            value="User has been using a bike helmet, which they got from the local bike shop.",
            source_message_indices=[0],
        ),
        ExtractedFact(
            memory_key="user_fifth_season_themes",
            value="User is interested in the themes of identity, culture and power in The Fifth Season.",
            source_message_indices=[1],
        ),
    ]
    out = _recover_specifics(facts, msgs)
    cost = [f for f in out if f.memory_key.startswith("user_bought_")]
    assert len(cost) == 1
    assert cost[0].value == "User bought my bike helmet for $120."
    assert cost[0].source_message_indices == [0]
    new = [f for f in out if f.memory_key.startswith("user_started_")]
    assert len(new) == 1
    assert new[0].value == 'User started listening to "The Power" by Naomi Alderman.'
    assert new[0].source_message_indices == [1]


def test_a_covered_start_is_not_duplicated() -> None:
    msgs = [{"role": "user", "content": BOOKS}]
    facts = [
        ExtractedFact(
            memory_key="user_started_the_power",
            value="User started listening to 'The Power' by Naomi Alderman.",
            source_message_indices=[0],
        )
    ]
    assert _recover_specifics(facts, msgs) == facts
