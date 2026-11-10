"""Line numbers of offsets in one text, by bisection in its newlines (found once): a
count from the top per offset is quadratic in a file of many sites."""

from __future__ import annotations

import bisect
import re


def newline_offsets(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def line_at(newlines: list[int], pos: int) -> int:
    """`pos`'s 1-based line, given its text's `newline_offsets`."""
    return bisect.bisect_left(newlines, pos) + 1
