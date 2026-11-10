"""Line numbers of offsets in one text, by bisection in its newlines (found once): a
count from the top per offset is quadratic in a file of many sites. Likewise the span
that holds a line, found by bisection in spans painted once per file instead of a scan of
every span per site."""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable


def newline_offsets(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def line_at(newlines: list[int], pos: int) -> int:
    """`pos`'s 1-based line, given its text's `newline_offsets`."""
    return bisect.bisect_left(newlines, pos) + 1


def first_span_at[T](spans: list[tuple[int, int, T]]) -> Callable[[int], T | None]:
    """A line -> the value of the first of `spans` (start line, end line, value) that holds
    it, None when none does: what a scan of the spans per line finds. The lines between
    two span ends are held by the same spans, so each such run is painted once, by the
    first span that holds it."""
    cuts = sorted({b for start, end, _v in spans if start <= end for b in (start, end + 1)})
    held: list[T | None] = [None] * len(cuts)  # held[i]: lines cuts[i] .. cuts[i + 1] - 1
    free = list(range(len(cuts) + 1))  # the first run at or after i not painted yet

    def first_free(i: int) -> int:
        root = i
        while free[root] != root:
            root = free[root]
        while free[i] != root:
            free[i], i = root, free[i]
        return root

    for start, end, value in spans:
        if start > end:
            continue
        i, stop = first_free(bisect.bisect_left(cuts, start)), bisect.bisect_left(cuts, end + 1)
        while i < stop:
            held[i] = value
            free[i] = i + 1
            i = first_free(i + 1)

    def at(line: int) -> T | None:
        i = bisect.bisect_right(cuts, line) - 1
        return held[i] if i >= 0 else None

    return at


def last_span_at[T](spans: list[tuple[int, int, T]]) -> Callable[[int], T | None]:
    """`first_span_at` with the last holding span winning: for spans sorted by start,
    widest first per start, the innermost."""
    return first_span_at(spans[::-1])
