"""A dropped relative phrase is recovered from the cited turn."""

from __future__ import annotations

import pytest

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.dates import _apply_temporal_anchor_backstop
from server.memory.domain.extraction_temporal import dropped_phrase_sentence, past_relative_phrase

SAID = "2022-08-22"  # Monday
TURN = (
    "I'm good! Was super nervous last week when I shared my book with my writers group "
    "but got some great feedback. My hard work is paying off, it's such an amazing feeling. "
    "Tomorrow I start the next chapter."
)
MSGS = [{"role": "user", "content": TURN, "timestamp": f"{SAID}T10:57:00"}]


def _fact(value: str, *, anchor: str | None = f"{SAID}T00:00:00", idx=(0,)) -> ExtractedFact:
    return ExtractedFact(
        memory_key="joanna_book_feedback",
        value=value,
        temporal_anchor=anchor,
        valid_at=anchor,
        source_message_indices=list(idx),
    )


def test_past_relative_phrase_ignores_present_and_future_forms() -> None:
    assert past_relative_phrase("nervous last week when I shared") == "last week"
    assert past_relative_phrase("we met yesterday at the park") == "yesterday"
    assert past_relative_phrase("went camping two weekends ago") == "two weekends ago"
    assert past_relative_phrase("Tomorrow I start the next chapter") is None
    assert past_relative_phrase("we are meeting next week") is None
    assert past_relative_phrase("busy this weekend") is None
    assert past_relative_phrase("John likes jazz") is None


def test_dropped_phrase_sentence_matches_the_right_sentence_only() -> None:
    hit = dropped_phrase_sentence(
        "Joanna shared her book with her writers group and received great feedback.", TURN
    )
    assert hit is not None and hit[1] == "last week" and "writers group" in hit[0]
    # a general statement that shares one word does not borrow the date
    assert dropped_phrase_sentence("Joanna feels amazing about her work.", TURN) is None
    # a value that already carries a date or its own phrase is not a candidate
    assert dropped_phrase_sentence("Joanna shared her book on 2022-08-15.", TURN) is None
    assert dropped_phrase_sentence("Joanna shared her book last week.", TURN) is None
    assert (
        dropped_phrase_sentence("", TURN) is None and dropped_phrase_sentence("x y z", "") is None
    )


def test_backstop_recovers_the_dropped_span_phrase() -> None:
    out = _apply_temporal_anchor_backstop(
        [_fact('"Joanna shared her book with her writers group and received great feedback."')],
        SAID,
        MSGS,
    )
    f = out[0]
    assert f.temporal_anchor == "2022-08-15T00:00:00"
    assert f.valid_at == "2022-08-15T00:00:00"
    assert f.value == (
        '"Joanna shared her book with her writers group and received great feedback '
        '(last week, around 2022-08-15)."'
    )


def test_backstop_recovers_a_point_phrase_with_on() -> None:
    msgs = [
        {
            "role": "user",
            "content": "Yesterday I chose to go to networking events to make things happen.",
            "timestamp": "2023-06-22T09:00:00",
        }
    ]
    out = _apply_temporal_anchor_backstop(
        [
            _fact(
                "Jon chose to go to networking events to make things happen.",
                anchor="2023-06-22T00:00:00",
            )
        ],
        "2023-06-22",
        msgs,
    )
    assert out[0].temporal_anchor == "2023-06-21T00:00:00"
    assert out[0].value.endswith("(yesterday, on 2023-06-21).")


def test_backstop_leaves_untouched_when_nothing_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    # the model's own earlier anchor is kept as valid_at; the phrase still decides the anchor
    own = _apply_temporal_anchor_backstop(
        [
            _fact(
                "Joanna shared her book with her writers group and received great feedback.",
                anchor="2022-08-10T00:00:00",
            )
        ],
        SAID,
        MSGS,
    )[0]
    assert own.temporal_anchor == "2022-08-15T00:00:00" and own.valid_at == "2022-08-10T00:00:00"
    # future phrase in the matching sentence: untouched
    future = _apply_temporal_anchor_backstop([_fact("Joanna starts the next chapter.")], SAID, MSGS)
    assert future[0].value == "Joanna starts the next chapter."
    # no cited messages: untouched
    plain = _apply_temporal_anchor_backstop(
        [
            _fact(
                "Joanna shared her book with her writers group and received great feedback.", idx=()
            )
        ],
        SAID,
        None,
    )
    assert plain[0].temporal_anchor == f"{SAID}T00:00:00"


def test_recovery_skips_values_with_their_own_ago_phrase_and_year_scale_durations() -> None:
    # the value carries "two weekends ago" itself (a form relative_phrase_class
    # does not name): not a candidate, the existing passes own it
    assert (
        dropped_phrase_sentence(
            "Melanie went camping with her family two weekends ago.",
            "We went camping with my fam two weekends ago. It was great.",
        )
        is None
    )
    # a year-scale duration on a state fact is not recovered (60-day window)
    msgs = [
        {
            "role": "user",
            "content": "I started playing acoustic guitar about five years ago; it's been a great way to express myself.",
            "timestamp": "2023-08-28T10:00:00",
        }
    ]
    out = _apply_temporal_anchor_backstop(
        [
            _fact(
                "Caroline started playing acoustic guitar as a way to express herself.",
                anchor="2023-08-28T00:00:00",
            )
        ],
        "2023-08-28",
        msgs,
    )
    assert out[0].temporal_anchor == "2023-08-28T00:00:00" and "(" not in out[0].value
