"""Extraction completeness on long sessions and attribute keys.

- one call emits about the same number of facts whatever the length, so a
  long dialogue drops facts (e.g. every "X is located in Chicago" line); past
  a size the session is read in windows;
- an age fact stored under a key without `_age` (`user_niece_maya`) cannot be
  addressed by facet completion, so keys carry the attribute suffix.
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.keys import attribute_of_value, normalize_attribute_keys
from server.memory.domain.topic_segment import Segment, size_segments


def _msgs(n_turns: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n_turns):
        out.append({"role": "user", "content": f"u{i}"})
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


def test_size_segments_are_fixed_turn_windows_that_never_split_a_turn() -> None:
    msgs = _msgs(110)  # 220 messages
    segs = size_segments(msgs, turns_per_segment=20)
    assert [s.turns for s in segs] == [20, 20, 20, 20, 20, 10]
    assert segs[0].start == 0 and segs[0].end == 40 and segs[1].start == 40
    assert segs[-1].end == len(msgs)
    # short session: one window
    assert size_segments(_msgs(5), turns_per_segment=20) == [
        Segment(idx=0, start=0, end=10, turns=5)
    ]
    # max_segments widens the window rather than dropping turns
    segs = size_segments(_msgs(200), turns_per_segment=20, max_segments=4)
    assert len(segs) == 4 and sum(s.turns for s in segs) == 200


def test_plan_segments_reads_a_long_session_in_size_windows(monkeypatch) -> None:
    from server.memory.app import pipeline as mp
    from server.memory.app import pipeline_state as pipeline_state
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_size_segment_messages", 60)
    monkeypatch.setattr(settings, "memory_extract_size_segment_turns", 20)
    long_state = pipeline_state.WorkflowState(
        org="o", namespace="n", agent="a", messages=_msgs(110)
    )
    segs = asyncio.run(mp._plan_segments(long_state))
    assert len(segs) == 6 and segs[0].end == 40
    short_state = pipeline_state.WorkflowState(
        org="o", namespace="n", agent="a", messages=_msgs(10)
    )
    assert len(asyncio.run(mp._plan_segments(short_state))) == 1
    monkeypatch.setattr(settings, "memory_extract_size_segment_messages", 0)
    assert len(asyncio.run(mp._plan_segments(long_state))) == 1  # 0 disables


def test_attribute_of_value_and_key_normalisation() -> None:
    assert attribute_of_value("User's niece Maya Fletcher is 26 years old.") == "age"
    assert attribute_of_value("Culinary Chat is based in Chicago, IL.") == "location"
    assert attribute_of_value("My aunt Savannah Pierce is from Atlanta, GA.") == "hometown"
    assert attribute_of_value("Craft Fest lasts for three days.") == "duration"
    assert attribute_of_value("Music Insights has a scale of four hundred people.") == "attendance"
    assert (
        attribute_of_value("User's boss Oliver Grant has a birthday on January 29th.") == "birthday"
    )
    assert attribute_of_value("User loves Italian food.") == ""
    facts = [
        ExtractedFact(
            memory_key="user_niece_maya", value="User's niece Maya Fletcher is 26 years old."
        ),
        ExtractedFact(memory_key="user_cousin_logan_age", value="Logan is 28 years old."),
        ExtractedFact(
            memory_key="theater_fest_duration_2", value="Theater Fest lasts for two weeks."
        ),
        ExtractedFact(
            memory_key="culinary_chat_venue", value="Culinary Chat is based in Chicago, IL."
        ),
        ExtractedFact(
            memory_key="user_aunt_savannah",
            value="User's aunt Savannah Pierce is from Atlanta, GA.",
        ),
        ExtractedFact(memory_key="user_food", value="User loves Italian food."),
    ]
    keys = [f.memory_key for f in normalize_attribute_keys(facts)]
    # a synonym suffix is canonicalised so one endsWith finds the whole set
    assert keys == [
        "user_niece_maya_age",
        "user_cousin_logan_age",
        "theater_fest_duration_2",
        "culinary_chat_location",
        "user_aunt_savannah_hometown",
        "user_food",
    ]
    more = normalize_attribute_keys(
        [
            ExtractedFact(
                memory_key="aquafest_turnout_2",
                value="AquaFest has a turnout of about eight hundred people.",
            ),
            ExtractedFact(
                memory_key="music_meetup_capacity",
                value="Music Meet-Up can accommodate about four hundred people.",
            ),
        ]
    )
    assert [f.memory_key for f in more] == ["aquafest_attendance_2", "music_meetup_attendance"]
