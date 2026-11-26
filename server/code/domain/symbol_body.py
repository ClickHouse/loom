"""Whole source spans assembled only from agreeing, retained raw lines."""

from __future__ import annotations

from typing import Any

UNMAPPABLE_SOURCE_ENDINGS = ("\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\u0085", "\u2028", "\u2029")
CONTINUED_LINE_PREFIX = "… continued at line "
CONTINUED_LINE_SUFFIX = " … line continues"


def retained_source_lines(row: dict[str, Any]) -> list[str] | None:
    """Physical source lines, excluding presentation prefixes and unmappable skeletons."""
    if row.get("kind") == "class_skeleton":
        return None
    text = str(row.get("content") or "")
    # A trailing non-LF separator can be a truncated physical line.
    if text.endswith(UNMAPPABLE_SOURCE_ENDINGS):
        return None
    # Index spans count LF boundaries; Unicode separators inside strings are text.
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    if any(
        line.rstrip().endswith(CONTINUED_LINE_SUFFIX)
        or line.lstrip().startswith(CONTINUED_LINE_PREFIX)
        for line in lines
    ):
        return None
    span = int(row.get("end_line") or 0) - int(row.get("start_line") or 0) + 1
    if span <= 0 or len(lines) < span:
        return None
    # Function chunks may prepend their doc comment or a continuation header.
    return lines[-span:]


def retained_source_window(
    rows: list[dict[str, Any]], start: int, end: int, *, anchor: int, revision: str
) -> tuple[int, int, str] | None:
    """Keep the contiguous retained lines around the anchor at its indexed revision."""
    by_line: dict[int, str] = {}
    gaps: set[int] = set()
    for row in rows:
        row_start, row_end = int(row["start_line"]), int(row["end_line"])
        span = range(max(start, row_start), min(end, row_end) + 1)
        if not span:
            continue
        lines = (
            retained_source_lines(row)
            if str(row.get("indexed_commit") or "").rstrip("\0") == revision
            else None
        )
        if lines is None:
            gaps.update(span)
            continue
        for line in span:
            text = lines[line - row_start]
            if by_line.setdefault(line, text) != text:
                gaps.add(line)
    for line in gaps:
        by_line.pop(line, None)
    if anchor not in by_line:
        return None
    start = end = anchor
    while by_line.get(start - 1, "").endswith("\n"):
        start -= 1
    while by_line[end].endswith("\n") and end + 1 in by_line:
        end += 1
    return start, end, "".join(by_line[line] for line in range(start, end + 1))


def complete_source_span(
    rows: list[dict[str, Any]],
    start: int,
    end: int,
    *,
    max_chars: int,
    own_declaration: bool = False,
) -> str:
    """The declared span without gaps or conflicting overlaps in its live chunks.

    Missing source stays missing. An empty result asks the caller to disclose elision.
    """
    if start <= 0 or end < start or (max_chars and end - start + 1 > max_chars):
        return ""
    if (
        start == end
        and not own_declaration
        and not all(
            int(r.get("start_line") or 0) == start
            and int(r.get("end_line") or 0) == end
            and r.get("kind") == "function"
            for r in rows
        )
    ):
        return ""
    by_line: dict[int, str] = {}
    for row in rows:
        lines = retained_source_lines(row)
        if lines is None:
            return ""
        for line_no, text in enumerate(lines, int(row["start_line"])):
            if line_no < start or line_no > end:
                continue
            if line_no in by_line and by_line[line_no] != text:
                return ""
            by_line[line_no] = text
    if len(by_line) != end - start + 1:
        return ""
    if any(not by_line[n].endswith("\n") for n in range(start, end)):
        return ""
    body = "".join(by_line[n] for n in range(start, end + 1))
    if max_chars and len(body) > max_chars:
        return ""
    return body
