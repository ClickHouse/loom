"""Size segmentation by characters as well as turns.

A short-but-heavy session (tool results carried whole) would reach one
extraction call under the message-count trigger alone."""

from __future__ import annotations

import asyncio
from typing import Any

from server.memory.domain.topic_segment import Segment, size_segments


def _msgs(n_turns: int, chars: int = 2) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n_turns):
        out.append({"role": "user", "content": f"u{i}"})
        out.append({"role": "assistant", "content": "a" * chars})
    return out


def test_char_budget_closes_a_window_early_and_never_splits_a_turn() -> None:
    msgs = _msgs(10, chars=10_000)  # 10 turns, ~10K chars each
    segs = size_segments(msgs, turns_per_segment=20, chars_per_segment=25_000)
    assert [s.turns for s in segs] == [2, 2, 2, 2, 2]
    assert segs[0].start == 0 and segs[0].end == 4 and segs[-1].end == len(msgs)


def test_a_turn_larger_than_the_budget_is_its_own_window() -> None:
    msgs = _msgs(3, chars=100_000)
    segs = size_segments(msgs, turns_per_segment=20, chars_per_segment=60_000)
    assert [s.turns for s in segs] == [1, 1, 1]


def test_zero_budget_keeps_turn_windows_only() -> None:
    msgs = _msgs(10, chars=10_000)
    assert size_segments(msgs, turns_per_segment=20, chars_per_segment=0) == [
        Segment(idx=0, start=0, end=20, turns=10)
    ]


def test_max_segments_still_bounds_the_fan_out() -> None:
    msgs = _msgs(40, chars=10_000)
    segs = size_segments(msgs, turns_per_segment=20, chars_per_segment=10_500, max_segments=4)
    assert len(segs) == 4 and sum(s.turns for s in segs) == 40 and segs[-1].end == len(msgs)


def test_plan_segments_triggers_on_characters(monkeypatch) -> None:
    from server.memory.app import pipeline as mp
    from server.memory.app import pipeline_state as pipeline_state
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_size_segment_messages", 60)
    monkeypatch.setattr(settings, "memory_extract_size_segment_turns", 20)
    monkeypatch.setattr(settings, "memory_extract_size_segment_chars", 60_000)
    heavy = pipeline_state.WorkflowState(
        org="o", namespace="n", agent="a", messages=_msgs(12, chars=15_000)
    )
    segs = asyncio.run(mp._plan_segments(heavy))
    assert len(segs) == 4 and heavy.segmenter == "size"
    light = pipeline_state.WorkflowState(org="o", namespace="n", agent="a", messages=_msgs(12))
    assert len(asyncio.run(mp._plan_segments(light))) == 1
    monkeypatch.setattr(settings, "memory_extract_size_segment_chars", 0)
    assert len(asyncio.run(mp._plan_segments(heavy))) == 1  # 0 disables
