"""A stated preference is a fact of its own."""

from __future__ import annotations

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.enrich import _recover_preferences
from server.memory.domain.extraction_preferences import (
    recover_stated_preferences,
    stated_preferences,
)

TURN = (
    "Besides great views, I also like hotels with unique features, such as a rooftop pool or a "
    "hot tub on the balcony. Do you know if any of these hotels have those features?"
)
MESSAGES = [
    {
        "role": "user",
        "content": "I'm booking The Edgewater Hotel in Seattle for the Space Needle package.",
    },
    {
        "role": "assistant",
        "content": "Great choice — the Edgewater's balcony rooms face the Needle.",
    },
    {"role": "user", "content": TURN},
]


def test_a_first_person_preference_is_found_and_the_question_is_not() -> None:
    prefs = stated_preferences(TURN)
    assert len(prefs) == 1
    _, verb, obj = prefs[0]
    assert verb.lower() == "like"
    assert obj.startswith("hotels with unique features")


def test_requests_and_hypotheticals_are_not_preferences() -> None:
    assert stated_preferences("I'd like you to summarise this.") == []
    assert stated_preferences("I like to know what you think, would you agree?") == []


def test_a_taste_no_fact_carries_becomes_its_own_row() -> None:
    facts = [
        ExtractedFact(
            memory_key="user_edgewater_hot_tub",
            value="User wants a room with a hot tub on the balcony at The Edgewater Hotel.",
            source_message_indices=[0],
        )
    ]
    out = _recover_preferences(facts, MESSAGES)
    new = [f for f in out if f.memory_key.startswith("user_preference_")]
    assert len(new) == 1
    assert new[0].value.startswith("User likes hotels with unique features")
    assert new[0].tags == ["preference"]
    assert new[0].source_message_indices == [2]


def test_a_taste_a_fact_already_carries_is_not_duplicated() -> None:
    facts = [
        ExtractedFact(
            memory_key="user_hotel_feature_preference",
            value="User likes hotels with unique features such as a rooftop pool or hot tub on the "
            "balcony, besides great views.",
            source_message_indices=[2],
        )
    ]
    assert _recover_preferences(facts, MESSAGES) == facts


def test_third_person_rewrite_for_fan_and_always_forms() -> None:
    def make(key: str, value: str, idx: int) -> tuple[str, str, int]:
        return (key, value, idx)

    msgs = [
        {"role": "user", "content": "I am a big fan of window seats on long flights."},
        {"role": "user", "content": "I always order the spicy tuna roll at sushi places."},
    ]
    out = recover_stated_preferences([], msgs, make)
    values = [v for _, v, _ in out]
    assert "User is a fan of window seats on long flights." in values
    assert "User always orders the spicy tuna roll at sushi places." in values
