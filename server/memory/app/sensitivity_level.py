"""A write's sensitivity level, read on the event loop in pieces when the value is long.

Detection is Python's `re` and Python code, which hold the GIL, so a worker thread would
not free the event loop: a long value is read in windows instead, with a turn for the
loop's other tasks between them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.memory.domain.sensitivity import (
    HIGH,
    SCALAR_SEP,
    SHAPE_SPAN,
    Sensitivity,
    caller_level,
    detect_sensitivity,
    detection_windows,
    detector_lines,
    detector_text,
    max_sensitivity,
)

# Detection reads up to about a second per megabyte (one pass per shape), so this many
# characters hold the event loop for about 20 ms at most. Past it, in one request, a
# value is read a window of this size at a time.
ON_LOOP_CHARS = 16 * 1024
assert ON_LOOP_CHARS >= 4 * SHAPE_SPAN  # a window moves past the one before it
# Lines of a decoded value read between two turns, however short they are.
_LINES_PER_TURN = 2_000


async def resolve_level(requested: str | None, text: str, *, read_before: int = 0) -> Sensitivity:
    """`resolve_sensitivity(requested, text)`, read in windows of `ON_LOOP_CHARS` with a
    turn between them once `read_before` (the characters this request's earlier values
    read) plus `text` pass `ON_LOOP_CHARS`."""
    level = caller_level(requested)
    if level is not None:
        return level
    if read_before + len(text) <= ON_LOOP_CHARS:
        return detect_sensitivity(text)
    found: Sensitivity = ""
    for window in detection_windows(text, ON_LOOP_CHARS):
        await asyncio.sleep(0)
        found = max_sensitivity([found, detect_sensitivity(window)])
        if found == HIGH:
            break
    return found


async def decoded_text(value: Any) -> str:
    """`detector_text(value)`. A long value's key walk takes a turn every
    `ON_LOOP_CHARS` characters or `_LINES_PER_TURN` lines; parsing its JSON is one call
    (about 10 ms for 8 MB)."""
    if isinstance(value, str) and len(value) <= ON_LOOP_CHARS:
        return detector_text(value)
    lines: list[str] = []
    chars = 0
    for line in detector_lines(value):
        lines.append(line)
        chars += len(line)
        if chars > ON_LOOP_CHARS or len(lines) % _LINES_PER_TURN == 0:
            chars = 0
            await asyncio.sleep(0)
    return SCALAR_SEP.join(lines)
