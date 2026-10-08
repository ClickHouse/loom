"""The deterministic resolver wins over the prompt's
"anchor to the observation date" default when the value carries a
closed-class relative phrase — and the resolver knows weekends.

On 1,996 LoCoMo facts, 349 values carried a relative phrase and 259 of them
were anchored to the session date with the resolver disagreeing on every one
("yesterday" anchored to the day it was said)."""

from __future__ import annotations

import pytest

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.dates import _apply_temporal_anchor_backstop
from server.memory.domain.extraction_temporal import resolve_relative_date

# Thursday 2023-07-13. Saturdays around it: 07-01, 07-08, (07-15).
OBS = "2023-07-13"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Melanie went camping with her family two weekends ago.", "2023-07-01"),
        ("went camping a couple of weekends ago", "2023-07-01"),
        ("went camping one weekend ago", "2023-07-08"),
        ("we met the weekend before last", "2023-07-01"),
        ("that was the week before last", "2023-06-29"),
        ("Caroline hiked over the weekend.", "2023-07-08"),
        ("we saw the show this weekend", "2023-07-08"),
        ("last weekend was the parade", "2023-07-08"),
        ("recently started a new job", None),
    ],
)
def test_resolver_knows_weekends(text: str, expected: str | None) -> None:
    assert resolve_relative_date(text, OBS) == expected


def test_this_weekend_on_a_saturday_or_sunday_is_that_weekend() -> None:
    assert resolve_relative_date("over the weekend", "2023-07-15") == "2023-07-15"  # Saturday
    assert resolve_relative_date("over the weekend", "2023-07-16") == "2023-07-15"  # Sunday


def _fact(key: str, value: str, anchor: str | None) -> ExtractedFact:
    return ExtractedFact(memory_key=key, value=value, temporal_anchor=anchor)


def test_backstop_overrides_the_session_date_default_but_not_a_real_anchor() -> None:
    facts = [
        # the prompt's default: activity anchored to the day it was said
        _fact("said_yesterday", "Caroline attended a support group yesterday.", f"{OBS}T00:00:00"),
        _fact("two_weekends", "Melanie went camping two weekends ago.", f"{OBS}T00:00:00"),
        # the model resolved it itself to another day: never touched
        _fact("model_own", "Caroline attended a parade last Friday.", "2023-07-07T00:00:00"),
        # anchored to the session date, no relative phrase: never touched
        _fact("plain_today", "Caroline is at the gym.", f"{OBS}T00:00:00"),
        # unanchored with a phrase: the original backstop
        _fact("unanchored", "John painted the fence last month.", None),
        # no phrase, no anchor
        _fact("none", "John likes jazz.", None),
    ]
    out = {f.memory_key: f for f in _apply_temporal_anchor_backstop(facts, OBS)}
    assert out["said_yesterday"].temporal_anchor == "2023-07-12T00:00:00"
    assert out["two_weekends"].temporal_anchor == "2023-07-01T00:00:00"
    assert out["model_own"].temporal_anchor == "2023-07-07T00:00:00"
    assert out["plain_today"].temporal_anchor == f"{OBS}T00:00:00"
    assert out["unanchored"].temporal_anchor == "2023-06-13T00:00:00"
    assert out["none"].temporal_anchor is None
    # the backstop touches anchors only; the text is the grounding pass's
    assert out["two_weekends"].value == "Melanie went camping two weekends ago."


def test_backstop_returns_the_same_list_when_nothing_changes() -> None:
    facts = [_fact("x", "John likes jazz.", None)]
    assert _apply_temporal_anchor_backstop(facts, OBS) is facts


def test_a_week_off_weekday_anchor_yields_to_the_resolver() -> None:
    """ "last Friday" said on Saturday 2023-07-15 is 07-14; the
    model wrote 07-07 (the Friday of the previous calendar week) into the
    anchor, valid_at and the text. All three move; nothing else does."""
    sat = "2023-07-15"
    facts = [
        ExtractedFact(
            memory_key="pottery",
            value="Melanie took her kids to a pottery workshop on 2023-07-07 (last Friday).",
            temporal_anchor="2023-07-07T00:00:00",
            valid_at="2023-07-07T00:00:00",
        ),
        # a weekday anchor that agrees with the resolver: untouched
        _fact("agrees", "Caroline attended a council meeting last Friday.", "2023-07-14T00:00:00"),
        # a weekday anchor off by something other than a week: the model's own
        _fact("odd", "Jon met a mentor last Tuesday (on 2023-07-05).", "2023-07-05T00:00:00"),
        # a non-weekday phrase with the model's own anchor: never touched
        _fact("yesterday_own", "Gina danced yesterday.", "2023-07-01T00:00:00"),
    ]
    out = {f.memory_key: f for f in _apply_temporal_anchor_backstop(facts, sat)}
    assert out["pottery"].temporal_anchor == "2023-07-14T00:00:00"
    assert out["pottery"].valid_at == "2023-07-14T00:00:00"
    assert out["pottery"].value == (
        "Melanie took her kids to a pottery workshop on 2023-07-14 (last Friday)."
    )
    assert out["agrees"].temporal_anchor == "2023-07-14T00:00:00"
    assert out["odd"].temporal_anchor == "2023-07-05T00:00:00"
    assert out["odd"].value == "Jon met a mentor last Tuesday (on 2023-07-05)."
    assert out["yesterday_own"].temporal_anchor == "2023-07-01T00:00:00"


def test_a_week_off_weekday_anchor_a_week_late_also_yields() -> None:
    # Thursday 2023-07-20: "last Tuesday" is 07-18; a model anchor on 07-25 is the
    # same convention error in the other direction and moves too.
    facts = [_fact("late", "Joined a group last Tuesday (on 2023-07-25).", "2023-07-25T00:00:00")]
    (out,) = _apply_temporal_anchor_backstop(facts, "2023-07-20")
    assert out.temporal_anchor == "2023-07-18T00:00:00"
    assert out.value == "Joined a group last Tuesday (on 2023-07-18)."
