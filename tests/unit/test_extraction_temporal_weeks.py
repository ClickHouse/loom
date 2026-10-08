"""A weekday inside a named week resolves deterministically, against the turn that said it."""

from __future__ import annotations

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.dates import _apply_temporal_anchor_backstop
from server.memory.domain.extraction_temporal import resolve_relative_date


def test_weekday_in_a_named_week() -> None:
    # Weeks run Monday–Sunday: from Friday 2024-10-04, next week is Oct 7–13.
    assert (
        resolve_relative_date("happening next week on Sunday at 2:00 PM", "2024-10-04")
        == "2024-10-13"
    )
    assert resolve_relative_date("Sunday next week at 3", "2024-10-04") == "2024-10-13"
    assert (
        resolve_relative_date("the week after next on Wednesday at 9 AM", "2024-10-02")
        == "2024-10-16"
    )
    assert resolve_relative_date("meeting this week on Friday", "2024-10-02") == "2024-10-04"
    assert resolve_relative_date("in two weeks", "2024-10-02") == "2024-10-16"
    # The older forms still win where they apply.
    assert resolve_relative_date("next Friday", "2024-10-02") == "2024-10-04"
    assert resolve_relative_date("I went there last Saturday", "2024-10-09") == "2024-10-05"


def _fact(value: str, anchor: str | None, idx: list[int]) -> ExtractedFact:
    return ExtractedFact(
        memory_key="k", value=value, temporal_anchor=anchor, source_message_indices=idx
    )


def test_backstop_anchors_to_the_turn_and_corrects_a_wrong_weekday() -> None:
    messages = [
        {"role": "user", "content": "I'm going to InnoTeach.", "timestamp": "2024-10-01T08:00:00"},
        {
            "role": "user",
            "content": "InnoTeach is happening next week on Sunday at 2:00 PM.",
            "timestamp": "2024-10-04T08:00:00",
        },
    ]
    # Model resolved the Sunday to a Tuesday: the anchor and the date in the text are corrected.
    f = _fact(
        "InnoTeach is happening on 2024-10-08 at 2:00 PM next week on Sunday.",
        "2024-10-08T00:00:00",
        [1],
    )
    out = _apply_temporal_anchor_backstop([f], "2024-10-01", messages)
    assert out[0].temporal_anchor == "2024-10-13T00:00:00"
    assert "2024-10-13" in out[0].value and "2024-10-08" not in out[0].value
    # An unanchored fact resolves against its own turn (2024-10-04), not the session date (2024-10-01).
    g = _fact("InnoTeach is happening next week on Sunday.", None, [1])
    assert (
        _apply_temporal_anchor_backstop([g], "2024-10-01", messages)[0].temporal_anchor
        == "2024-10-13T00:00:00"
    )
    # A model anchor on the RIGHT weekday is the model's own resolution and stays.
    h = _fact("InnoTeach is happening next week on Sunday.", "2024-10-20T00:00:00", [1])
    assert (
        _apply_temporal_anchor_backstop([h], "2024-10-01", messages)[0].temporal_anchor
        == "2024-10-20T00:00:00"
    )
    # The value dropped the weekday but the cited turn names it: the turn decides.
    v = _fact(
        "InnoTeach is happening on 2024-10-08 at 2:00 PM next week.", "2024-10-08T00:00:00", [1]
    )
    outv = _apply_temporal_anchor_backstop([v], "2024-10-01", messages)[0]
    assert outv.temporal_anchor == "2024-10-13T00:00:00" and "2024-10-13" in outv.value
    # No phrase anywhere → untouched.
    plain = [
        {
            "role": "user",
            "content": "InnoTeach is on 2024-10-08.",
            "timestamp": "2024-10-04T08:00:00",
        }
    ]
    k = _fact("InnoTeach is on 2024-10-08.", "2024-10-08T00:00:00", [0])
    assert _apply_temporal_anchor_backstop([k], "2024-10-01", plain)[0] is k
