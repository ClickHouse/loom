"""Immediacy phrases anchor to the turn's day."""

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.dates import clamp_immediacy_anchors


def _f(value, anchor, conf):
    return ExtractedFact(
        memory_key="k", value=value, temporal_anchor=anchor, temporal_confidence=conf
    )


def test_a_just_got_back_fact_anchored_weeks_early_takes_the_session_day() -> None:
    out = clamp_immediacy_anchors(
        [
            _f(
                "User just got back from an amazing day at Universal Studios Hollywood.",
                "2023-06-24",
                0.0,
            )
        ],
        "2023-07-15",
    )
    assert out[0].temporal_anchor == "2023-07-15" and out[0].temporal_confidence == 0.5


def test_pinned_relative_and_unanchored_facts_pass_through() -> None:
    facts = [
        _f("User just got back from Paris.", "2023-06-24", 1.0),
        _f("User went to a birthday dinner a month ago.", "2023-06-15", 0.0),
        _f("User just finished a marathon.", None, 0.0),
        _f("User just got back from the gym.", "2023-07-13", 0.0),
    ]
    out = clamp_immediacy_anchors(facts, "2023-07-15")
    assert [f.temporal_anchor for f in out] == ["2023-06-24", "2023-06-15", None, "2023-07-13"]


def test_yesterday_resolves_to_the_day_before() -> None:
    out = clamp_immediacy_anchors(
        [_f("User watched the finale yesterday.", "2023-05-01", 0.3)], "2023-07-15"
    )
    assert out[0].temporal_anchor == "2023-07-14"
    assert clamp_immediacy_anchors([], "2023-07-15") == []
