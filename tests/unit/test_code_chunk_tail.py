"""A line-window file keeps its last lines — a manifest's
closing braces past a window boundary are not a chunk too small to keep."""

from __future__ import annotations

from server.code.domain.extract.chunker import _FALLBACK_WINDOW_LINES, _fallback_chunks


def test_a_short_tail_past_the_window_rides_with_the_last_window() -> None:
    lines = (
        ["{\n"]
        + [f'  "k{i}": "{"v" * 20}",\n' for i in range(_FALLBACK_WINDOW_LINES - 2)]
        + ['  "z": 1\n', "}\n"]
    )
    assert len(lines) == _FALLBACK_WINDOW_LINES + 1
    chunks = _fallback_chunks("packages/cli/package.json", lines)
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, _FALLBACK_WINDOW_LINES + 1)]
    assert "".join(c.content for c in chunks) == "".join(lines)


def test_a_long_tail_is_its_own_window_as_before() -> None:
    lines = [f"line {i} {'x' * 30}\n" for i in range(_FALLBACK_WINDOW_LINES + 10)]
    chunks = _fallback_chunks("README.md", lines)
    assert [(c.start_line, c.end_line) for c in chunks] == [
        (1, _FALLBACK_WINDOW_LINES),
        (_FALLBACK_WINDOW_LINES + 1, _FALLBACK_WINDOW_LINES + 10),
    ]
