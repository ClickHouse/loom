"""The extraction prompt keeps enumerable attribute sets whole.

Each member of a set (every event's duration, every person's birthday) is its
own memory, and keys end with the attribute (`_duration`, `_birthday`, `_age`,
`_team_size`) so facet completion can address the set by suffix.
"""

from __future__ import annotations

import re

from server.memory.domain.extraction_prompts import _EXTRACTION_SYSTEM_PROMPT as _RAW

# The prompt is hand-wrapped; compare on collapsed whitespace.
P = re.sub(r"\s+", " ", _RAW)


def test_third_party_entities_are_facts() -> None:
    assert "Third-party entities" in P
    assert "InnoLaw 2024 lasts for eight weeks" in P
    assert "innolaw_2024_duration" in P


def test_enumerable_sets_are_kept_whole_with_attribute_suffix_keys() -> None:
    assert "Enumerable attribute sets" in P
    assert "keep EVERY (entity, attribute) pair" in P
    assert "MUST END with the attribute name" in P
    for key in ("corp_law_convo_duration", "user_cousin_logan_age", "fintech_lab_attendance"):
        assert key in P
    assert "never `user_event_corp_law_convo` for a duration" in P
    assert "One attribute per fact" in P


def test_restatement_rule_does_not_swallow_sibling_values() -> None:
    assert 'never a "restatement" of another entity\'s value' in P


def test_narrated_observations_are_facts() -> None:
    """A third-person or literary session about the user
    yields events and the emotional state as dated facts, never an empty
    list."""
    assert "NARRATED OBSERVATIONS" in P
    assert "the user's emotional state as its own fact" in P
    assert 'Never return {"facts": []} for a narrated session' in P


def test_relative_expressions_resolve_against_the_turn_clock() -> None:
    assert "resolve ITS relative expressions" in P
    assert "against THAT message's date, not against the session's observation_date" in P
    assert "three announcements of an event's time are three dated facts" in P
