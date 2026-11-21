"""Raw source contexts for complete original-unit correspondence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceOrigins:
    """Caller-authenticated raw sources; prepared containers remain exact paid bytes."""

    historical_raw: str
    current_raw: str
    language: str


def _project(point: int, edits: list[tuple[int, int, str]]) -> int | None:
    shift = 0
    for start, end, replacement in edits:
        if point <= start:
            break
        if point < end:
            return None
        shift += len(replacement) - (end - start)
    return point + shift


def _prepared_positions(raw: str, prepared, language: str) -> tuple[int | None, ...]:
    # Import lazily: the preparer also imports the lifecycle and planner's dependencies.
    from collections import Counter

    from scripts.i18n import translate as t

    if language not in {lang.code for lang in t.LANGUAGES}:
        raise ValueError("source origins name an unknown language")
    positions: list[int | None] = list(range(len(raw) + 1))
    placeholders = t.language_placeholder(raw)
    text = t.replace_spans(raw, placeholders)
    positions = [_project(p, placeholders) for p in positions]
    before, after = t.link_targets(text), t.link_targets(prepared.english)
    if len(before) != len(after):
        raise ValueError("prepared link occurrences differ from their raw source")
    edits = []
    for (a, b, link, image), (_, _, target, target_image) in zip(before, after, strict=True):
        if image != target_image or target not in t.link_routes(
            link, image, prepared.path, language
        ):
            raise ValueError("prepared link target has no matching raw source origin")
        edits.append((a, b, target))
    text = t.replace_spans(text, edits)
    positions = [_project(p, edits) if p is not None else None for p in positions]
    insertions: list[tuple[int, int, str]] = []
    anchored = t.anchor_headings(text, Counter(), insertions=insertions)
    if anchored != prepared.english:
        raise ValueError("prepared container differs outside maintained source transformations")
    return tuple(_project(p, insertions) if p is not None else None for p in positions)


def _line_embeddings(old: list[str], new: list[str]) -> tuple[list[int | None], list[int | None]]:
    # Missing lines may change only inside endpoints proved by retained neighbours.
    available = set(new)
    first: list[int | None] = []
    last: list[int | None] = []
    at = 0
    for line in old:
        if line not in available:
            first.append(None)
            continue
        while at < len(new) and new[at] != line:
            at += 1
        if at == len(new):
            raise ValueError("retained raw source lines are not in historical order")
        first.append(at)
        at += 1
    at = len(new) - 1
    for line in reversed(old):
        if line not in available:
            last.append(None)
            continue
        while at >= 0 and new[at] != line:
            at -= 1
        if at < 0:
            raise ValueError("retained raw source lines are not in historical order")
        last.append(at)
        at -= 1
    return first, list(reversed(last))


def _terminal_append(historical, current, units, sources, positions, current_positions) -> bool:
    from scripts.i18n import chunks

    before, after = sources.historical_raw, sources.current_raw
    # Maintained link rewrites can change the prepared prefix without changing raw ownership.
    if (
        not units
        or not before.endswith("\n")
        or len(after) <= len(before)
        or not after.startswith(before)
        or current_positions[len(before)] is None
        or not current_positions[len(before)] < len(current.english)
        or positions[-1] != len(historical.english)
        or units[-1].end != len(historical.english)
        or not units[-1].start < units[-1].end
    ):
        return False
    suffix = after[len(before) :]
    if not suffix.endswith("\n") or not (before.endswith("\n\n") or suffix.startswith("\n")):
        return False
    # Site attributes may use braces inline or without a colon.
    if any(brace in suffix for brace in "{}"):
        return False
    # Indentation can continue a site-extension list even outside CommonMark's list.
    if any(line.strip() and line != line.lstrip() for line in suffix.splitlines()):
        return False
    parser, old_tokens = chunks._github_parse(before)
    _, current_tokens = chunks._github_parse(after)
    environment = {}
    appended = parser.parse(suffix, environment)
    if not appended or environment.get("references"):
        return False
    # Container source maps may absorb blank separators; their content must not change.
    fields = ("type", "tag", "nesting", "level", "content", "markup", "attrs", "info", "hidden")
    if [tuple(getattr(t, field) for field in fields) for t in old_tokens] != [
        tuple(getattr(t, field) for field in fields) for t in current_tokens[: len(old_tokens)]
    ]:
        return False
    added = current_tokens[len(old_tokens) :]
    paragraph = (("paragraph_open", 0), ("inline", 1), ("paragraph_close", 0))
    if (
        not added
        or len(added) % 3
        or any((token.type, token.level) != paragraph[i % 3] for i, token in enumerate(added))
    ):
        return False
    if any(
        child.type in {"html_inline", "image"} for token in added for child in token.children or []
    ):
        return False
    if chunks._site_lists(suffix, []) or any(
        line.lstrip().startswith(("!!!", "???")) for line in suffix.splitlines()
    ):
        return False
    heading_lines = next(
        (t.map for t in reversed(old_tokens) if t.type == "heading_open" and t.level == 0),
        None,
    )
    if heading_lines is None:
        return False
    heading = positions[chunks._line_starts(before)[heading_lines[0]]]
    return heading is not None and units[-1].start <= heading < units[-1].end


def _raw_projection(
    old: str, new: str, points: set[int], *, terminal_append: bool = False
) -> dict[int, int]:
    from bisect import bisect_right
    from itertools import accumulate

    before, after = old.splitlines(keepends=True), new.splitlines(keepends=True)
    first, last = _line_embeddings(before, after)
    starts = [0, *accumulate(map(len, before))]
    targets = [0, *accumulate(map(len, after))]
    projected = {}
    for point in points:
        # Proven terminal ownership also covers repeated trailing blank-line neighbours.
        if terminal_append and point == len(old):
            projected[point] = len(new)
            continue
        index = bisect_right(starts, point) - 1
        boundary = point in starts
        neighbours = (
            [i for i in (index - 1, index) if 0 <= i < len(before)] if boundary else [index]
        )
        if not neighbours or any(first[i] is None or first[i] != last[i] for i in neighbours):
            raise ValueError("raw source endpoint has missing or ambiguous retained origins")
        if boundary:
            previous = first[index - 1] if index else None
            following = first[index] if index < len(before) else None
            left = targets[previous + 1] if previous is not None else 0
            right = targets[following] if following is not None else len(new)
            if left != right:
                raise ValueError("raw insertion lies at an original unit or separator boundary")
            projected[point] = left
        else:
            origin = first[index]
            assert origin is not None
            projected[point] = targets[origin] + point - starts[index]
    return projected


def counterpart_ranges(historical, current, units, sources: SourceOrigins):
    """Compute total counterpart cuts from unique raw origins, never supplied offsets.

    Callers still authenticate raw Git bytes, locale and original paid request evidence.
    The range proof does not admit calls or certify any translated reply.
    """
    if any(unit.reply_ref is None for unit in units):
        raise ValueError("source origin correspondence requires completed original units")
    return partition_counterparts(historical, current, units, sources)


def partition_counterparts(historical, current, units, sources: SourceOrigins):
    """Project a caller-validated complete partition without claiming completions."""
    old = _prepared_positions(sources.historical_raw, historical, sources.language)
    new = _prepared_positions(sources.current_raw, current, sources.language)
    inverse, ambiguous = {}, set()
    for point, prepared in enumerate(old):
        if prepared is not None:
            if prepared in inverse:
                ambiguous.add(prepared)
            inverse[prepared] = point
    cuts = {p for unit in units for p in (unit.start, unit.end)}
    if any(p not in inverse or p in ambiguous for p in cuts):
        raise ValueError("original cut falls inside a source transformation")
    terminal_append = new[-1] == len(current.english) and _terminal_append(
        historical, current, units, sources, old, new
    )
    raw = _raw_projection(
        sources.historical_raw,
        sources.current_raw,
        {inverse[p] for p in cuts},
        terminal_append=terminal_append,
    )
    ranges = []
    previous_old = previous_new = 0
    for unit in units:
        start, end = (new[raw[inverse[p]]] for p in (unit.start, unit.end))
        if (
            start is None
            or end is None
            or not 0 <= previous_new <= start < end <= len(current.english)
        ):
            raise ValueError("current counterpart has invalid source origins")
        before = historical.english[previous_old : unit.start]
        after = current.english[previous_new:start]
        if before != after or before not in {"", "\n"}:
            raise ValueError("source origin correspondence changes separator ownership")
        ranges.append((start, end))
        previous_old, previous_new = unit.end, end
    if previous_old != len(historical.english) or previous_new != len(current.english):
        raise ValueError("source origin correspondence does not cover the complete containers")
    return ranges
