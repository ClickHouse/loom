"""A stated age is a fact of its own."""

from __future__ import annotations

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.enrich import _recover_age
from server.memory.domain.extraction_attributes import stated_age

TURN = (
    "I'm considering pursuing the CDMP certification to enhance my skills. As a 32-year-old "
    "Digital Marketing Specialist with five years of experience, is it worth it?"
)


def test_first_person_ages_are_found_and_others_are_not() -> None:
    assert stated_age(TURN) == "32"
    assert stated_age("I'm 32 and officially in my early thirties.") == "32"
    assert stated_age("I turned 40 last week.") == "40"
    assert (
        stated_age("And, by the way, do you think 32 is considered young for Europe trips?") == "32"
    )
    assert stated_age("My grandma's 75th birthday celebration was lovely.") is None
    assert stated_age("I'm 20 minutes away.") is None


def test_an_age_no_fact_carries_becomes_the_user_age_row() -> None:
    msgs = [{"role": "user", "content": TURN, "timestamp": "2023-05-25T18:00:00"}]
    facts = [
        ExtractedFact(
            memory_key="user_cdmp_interest",
            value="User is considering the CDMP certification to enhance their marketing skills.",
            source_message_indices=[0],
        )
    ]
    out = _recover_age(facts, msgs)
    age = [f for f in out if f.memory_key == "user_age"]
    assert len(age) == 1
    assert age[0].value == "User is 32 years old (as of 2023-05-25)."
    assert age[0].source_message_indices == [0]


def test_an_age_a_fact_already_carries_is_not_duplicated() -> None:
    msgs = [{"role": "user", "content": TURN}]
    facts = [
        ExtractedFact(
            memory_key="user_profile",
            value="User is a 32-year-old Digital Marketing Specialist.",
            source_message_indices=[0],
        )
    ]
    assert _recover_age(facts, msgs) == facts
