"""An answer is scored on what it cites."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.domain.answer_grounding import grounding_of
from server.memory.domain.question_window import TimeWindow


def _m(key, sess, date):
    return SimpleNamespace(memory_key=key, session_id=sess, valid_at=date, temporal_anchor="")


MEMS = [
    _m("a", "s1", "2023-05-05 10:00:00"),
    _m("b", "s1", "2023-05-06 10:00:00"),
    _m("c", "s2", "2023-01-01 10:00:00"),
    _m("d", "", ""),
]


def test_sessions_and_concentration():
    g = grounding_of(["a", "b", "zzz"], MEMS)
    assert g.cited == 2 and g.cited_sessions == ["s1"] and g.citation_concentration == 1.0
    assert g.temporal_consistency is None  # no window
    g2 = grounding_of(["a", "c"], MEMS)
    assert g2.cited_sessions == ["s1", "s2"] and g2.citation_concentration == 0.5


def test_temporal_consistency_against_the_window():
    w = TimeWindow("2023-05-01", "2023-05-10", "span")
    inside = grounding_of(["a", "b"], MEMS, window=w)
    outside = grounding_of(["c"], MEMS, window=w)
    mixed = grounding_of(["a", "c"], MEMS, window=w)
    assert inside.temporal_consistency > 0.8
    assert outside.temporal_consistency < -0.4
    assert outside.temporal_consistency < mixed.temporal_consistency < inside.temporal_consistency
    # undated citations do not count toward the mean
    assert grounding_of(["d"], MEMS, window=w).temporal_consistency is None
    assert grounding_of(["d"], MEMS, window=w).cited == 1


def test_open_window_gives_no_consistency():
    w = TimeWindow("", "2023-06-01", "open_past")
    assert grounding_of(["a"], MEMS, window=w).temporal_consistency is None
