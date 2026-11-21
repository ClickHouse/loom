"""Whole source spans assembled only from agreeing, retained raw lines."""

from __future__ import annotations

from typing import Any


def retained_source_lines(row: dict[str, Any]) -> list[str] | None:
    """Physical source lines, excluding presentation prefixes and unmappable skeletons."""
    if row.get("kind") == "class_skeleton":
        return None
    text = str(row.get("content") or "")
    # A trailing non-LF separator can be a truncated physical line.
    if text.endswith(("\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\u0085", "\u2028", "\u2029")):
        return None
    # Index spans count LF boundaries; Unicode separators inside strings are text.
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    if any(
        line.rstrip().endswith(" … line continues")
        or line.lstrip().startswith("… continued at line ")
        for line in lines
    ):
        return None
    span = int(row.get("end_line") or 0) - int(row.get("start_line") or 0) + 1
    if span <= 0 or len(lines) < span:
        return None
    # Function chunks may prepend their doc comment or a continuation header.
    return lines[-span:]


def complete_source_span(
    rows: list[dict[str, Any]], start: int, end: int, *, max_chars: int
) -> str:
    """The declared span without gaps or conflicting overlaps in its live chunks.

    Missing source stays missing. An empty result asks the caller to disclose elision.
    """
    if start <= 0 or end < start or end - start + 1 > max_chars:
        return ""
    if start == end and not all(
        int(r.get("start_line") or 0) == start
        and int(r.get("end_line") or 0) == end
        and r.get("kind") == "function"
        for r in rows
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
    if len(body) > max_chars:
        return ""
    return body
