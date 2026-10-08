"""Grounding relative phrases in the stored fact text:
classification, the appended note's precision, and the post-anchor pass."""

from __future__ import annotations

import pytest

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.dates import _apply_relative_date_grounding
from server.memory.domain.extraction_temporal import (
    duration_note,
    ground_relative_phrase,
    relative_phrase_class,
    span_note,
)


@pytest.mark.parametrize(
    "text,cls",
    [
        ("Calvin attended a fancy gala in Boston yesterday.", "point"),
        ("Evan's son fell off his bike last Tuesday", "point"),
        ("Jolene practiced yoga on top of mount Talkeetna yesterday morning.", "point"),
        ("Nate dyed his hair last week.", "span"),
        ("User moved to Denver two months ago", "span"),
        ("Deborah's mother passed away a few years ago.", "vague"),
        ("User recently started a pottery class", "vague"),
        ("User is a big fan of coconut milk-based sauces.", ""),
        (
            "On 2023-11-16 Calvin attended a gala yesterday",
            "point",
        ),  # class only; grounding skips ISO text
    ],
)
def test_relative_phrase_class(text: str, cls: str) -> None:
    assert relative_phrase_class(text) == cls


def test_ground_point_and_span_and_vague_precision() -> None:
    assert (
        ground_relative_phrase(
            '"Calvin attended a fancy gala in Boston yesterday."', "2023-11-16T00:00:00"
        )
        == '"Calvin attended a fancy gala in Boston yesterday (on 2023-11-16)."'
    )
    assert ground_relative_phrase("Nate dyed his hair last week.", "2022-04-08") == (
        "Nate dyed his hair last week (around 2022-04-08)."
    )
    # With the session date the span reads against the day it was said,
    # never against the resolved day: a reader takes "(around 2023-06-02)" as
    # one more week back.
    assert ground_relative_phrase("Nate dyed his hair last week.", "2022-04-08", "2022-04-15") == (
        "Nate dyed his hair last week (the week before 2022-04-15)."
    )
    assert ground_relative_phrase(
        "Melanie went camping two weeks ago.", "2023-07-03", "2023-07-17T10:00:00"
    ) == ("Melanie went camping two weeks ago (about two weeks before 2023-07-17).")
    assert ground_relative_phrase(
        "Deborah's mother passed away a few years ago.", "2020-01-23T00:00:00"
    ) == ("Deborah's mother passed away a few years ago (around 2020).")
    assert ground_relative_phrase("User moved a couple of months back", "2023-03-10") == (
        "User moved a couple of months back (around 2023-03)"
    )


@pytest.mark.parametrize(
    ("text", "session", "note"),
    [
        ("Caroline spoke at a school event last week.", "2023-06-09", "the week before 2023-06-09"),
        ("They hiked last weekend.", "2023-06-09T00:00:00", "the weekend before 2023-06-09"),
        ("She started the job last month.", "2023-06-09", "the month before 2023-06-09"),
        ("He will move next year.", "2023-06-09", "the year after 2023-06-09"),
        ("This past week was rough.", "2023-06-09", "the week before 2023-06-09"),
        ("Melanie went camping 3 weeks ago.", "2023-06-09", "about 3 weeks before 2023-06-09"),
        ("Melanie went camping one month ago.", "2023-06-09", "about one month before 2023-06-09"),
        ("Melanie went camping last week.", "", None),
        ("Melanie went camping last week.", "unknown", None),
        ("Melanie went camping yesterday.", "2023-06-09", None),
    ],
)
def test_span_note_reads_against_the_session_date(
    text: str, session: str, note: str | None
) -> None:
    assert span_note(text, session) == note


def test_ground_is_a_no_op_without_phrase_anchor_or_when_dated() -> None:
    assert ground_relative_phrase("User likes tea.", "2023-01-01") == "User likes tea."
    assert ground_relative_phrase("User ran yesterday", "") == "User ran yesterday"
    assert ground_relative_phrase("User ran yesterday", "garbage") == "User ran yesterday"
    already = "Nate dyed his hair last week (around 2022-04-08)."
    assert ground_relative_phrase(already, "2022-04-08") == already
    assert ground_relative_phrase("On 2023-11-16 (yesterday) Calvin went out", "2023-11-16") == (
        "On 2023-11-16 (yesterday) Calvin went out"
    )


def _fact(value: str, anchor: str | None, conf: float = 1.0) -> ExtractedFact:
    return ExtractedFact(
        memory_key="k", value=value, temporal_anchor=anchor, temporal_confidence=conf
    )


def test_apply_grounding_rewrites_anchored_relative_facts_only() -> None:
    facts = [
        _fact('"Calvin attended a gala yesterday."', "2023-11-16T00:00:00"),
        _fact("Nate dyed his hair last week.", None),  # no anchor → untouched
        _fact("User likes tea.", "2023-01-01T00:00:00"),  # no phrase → untouched
        _fact("Deborah's mother passed away a few years ago.", "2020-01-23T00:00:00", conf=1.0),
    ]
    out = _apply_relative_date_grounding(facts)
    assert out[0].value == '"Calvin attended a gala yesterday (on 2023-11-16)."'
    assert out[0].temporal_confidence == 1.0 and out[0].temporal_anchor == "2023-11-16T00:00:00"
    assert out[1] is facts[1] and out[2] is facts[2]
    assert out[3].value.endswith("(around 2020).")
    assert out[3].temporal_confidence == 0.5  # vague offset: the anchor is an estimate


def test_apply_grounding_returns_same_list_when_nothing_changes() -> None:
    facts = [_fact("User likes tea.", "2023-01-01T00:00:00"), _fact("User ran yesterday", None)]
    assert _apply_relative_date_grounding(facts) is facts


@pytest.mark.parametrize(
    ("text", "session", "note"),
    [
        # The LoCoMo evidence turns behind the "when did X start" temporal
        # questions, with the gold answer's shape.
        (
            "John had a dog named Max who was part of his family for 10 years.",
            "2023-06-03",
            "since about 2013, as of 2023-06-03",
        ),
        (
            "Nate has had his first two turtles for 3 years now.",
            "2022-01-21",
            "since about 2019, as of 2022-01-21",
        ),
        (
            "Tim has been playing the violin for about four months now.",
            "2023-12-06",
            "since about 2023-08, as of 2023-12-06",
        ),
        (
            "Audrey has had her dogs for 3 years.",
            "2023-03-27",
            "since about 2020, as of 2023-03-27",
        ),
        (
            "John has been playing drums for a month now.",
            "2022-03-27T00:00:00",
            "since about 2022-02, as of 2022-03-27",
        ),
        (
            "Jolene and her partner have been together for three years.",
            "2023-02-25",
            "since about 2020, as of 2023-02-25",
        ),
        (
            "Evan has been working on his health for two years now.",
            "2023-08-07",
            "since about 2021, as of 2023-08-07",
        ),
        (
            "Maria has been volunteering for about a year.",
            "2023-08-03",
            "since about 2022, as of 2023-08-03",
        ),
        (
            "Sam has run the club for the past two weeks.",
            "2023-08-15",
            "since about 2023-08-01, as of 2023-08-15",
        ),
        (
            "They have lived there for half a year.",
            "2023-08-15",
            "since about 2023-02, as of 2023-08-15",
        ),
        ("She has worked there for a decade.", "2023-08-15", "since about 2013, as of 2023-08-15"),
        # Not a history: a plan, a year already in the text, no session day, no phrase.
        ("Sam will stay for the next two years.", "2023-08-15", None),
        ("Jolene has been doing yoga since 2020, for 3 years.", "2023-06-06", None),
        ("Nate has had his turtles for 3 years.", "", None),
        ("Nate got his turtles three years ago.", "2022-01-21", None),
        ("Nate has two turtles.", "2022-01-21", None),
    ],
)
def test_duration_note_names_the_implied_start(text: str, session: str, note: str | None) -> None:
    assert duration_note(text, session) == note


def test_apply_grounding_appends_the_duration_start_with_or_without_an_anchor() -> None:
    facts = [
        _fact("Audrey has had her dogs for 3 years.", "2020-03-27T00:00:00"),
        _fact("Nate has had his first two turtles for 3 years now.", None),
        _fact('"Jolene has been doing yoga since 2020, for 3 years."', "2020-08-21"),
    ]
    out = _apply_relative_date_grounding(facts, "2023-03-27T00:00:00")
    assert (
        out[0].value == "Audrey has had her dogs for 3 years (since about 2020, as of 2023-03-27)."
    )
    assert out[0].temporal_anchor == "2020-03-27T00:00:00"
    assert (
        out[1].value
        == "Nate has had his first two turtles for 3 years now (since about 2020, as of 2023-03-27)."
    )
    assert out[2].value == facts[2].value
