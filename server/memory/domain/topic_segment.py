"""Segments of a conversation before extraction.

A long session read by one extraction call is summarised into roughly the
same number of facts whatever its length, so past a size the session is read
in fixed windows of whole turns (a user message plus the assistant messages
that follow it), one call per window. `size_segments` is the planner
`memory_pipeline._plan_segments` calls. Windows are fixed rather than cut at
embedding-similarity topic boundaries, whose gain did not reproduce; the module
name is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Segment:
    idx: int
    start: int  # first message index (inclusive)
    end: int  # last message index (exclusive)
    turns: int


def turn_ranges(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Split messages into turns: each turn starts at a user message and runs
    to the next user message. Leading non-user messages form their own turn.
    Returns [start, end) message index ranges, in order."""
    if not messages:
        return []
    starts = [i for i, m in enumerate(messages) if str(m.get("role", "")) == "user"]
    if not starts or starts[0] != 0:
        starts = [0, *starts]
    ranges = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(messages)
        if e > s:
            ranges.append((s, e))
    return ranges


def turn_chars(messages: list[dict[str, Any]], rng: tuple[int, int]) -> int:
    return sum(len(str(m.get("content", "") or "")) for m in messages[rng[0] : rng[1]])


def size_segments(
    messages: list[dict[str, Any]],
    *,
    turns_per_segment: int,
    max_segments: int = 16,
    chars_per_segment: int = 0,
) -> list[Segment]:
    """Fixed windows of `turns_per_segment` turns: the
    completeness fallback for a long session.
    One extraction call reads a bounded number of turns, so a 220-message
    dialogue is not summarised into the ~36 facts one call tends to emit.
    Windows never split a turn; the last window absorbs the remainder when
    `max_segments` would otherwise be exceeded.

    `chars_per_segment` closes a window early once its turns
    carry that many characters: an agent transcript whose tool results ride
    along runs ~190K chars a session at the median with 25 turns, so a turn
    budget alone hands one extraction call the whole thing. A single turn
    larger than the budget is a window of its own. 0 = turns only."""
    ranges = turn_ranges(messages)
    if not ranges or turns_per_segment <= 0:
        return [Segment(idx=0, start=0, end=len(messages), turns=len(ranges))]
    total_chars = turn_chars(messages, (0, len(messages)))
    if len(ranges) <= turns_per_segment and not (
        chars_per_segment > 0 and total_chars > chars_per_segment
    ):
        return [Segment(idx=0, start=0, end=len(messages), turns=len(ranges))]
    per = max(turns_per_segment, -(-len(ranges) // max(1, max_segments)))
    windows: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    cur_chars = 0
    for rng in ranges:
        c = turn_chars(messages, rng)
        full = len(cur) >= per or (
            chars_per_segment > 0 and cur and cur_chars + c > chars_per_segment
        )
        if full:
            windows.append(cur)
            cur, cur_chars = [], 0
        cur.append(rng)
        cur_chars += c
    if cur:
        windows.append(cur)
    # `max_segments` bounds the fan-out: past it the last window absorbs the
    # remainder (as the turn budget alone would).
    if len(windows) > max(1, max_segments):
        head, tail = windows[: max_segments - 1], windows[max_segments - 1 :]
        windows = [*head, [r for w in tail for r in w]]
    return [
        Segment(idx=k, start=w[0][0], end=w[-1][1], turns=len(w)) for k, w in enumerate(windows)
    ]


__all__ = ["Segment", "size_segments", "turn_chars", "turn_ranges"]
