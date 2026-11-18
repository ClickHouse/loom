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


def _line_embeddings(old: list[str], new: list[str]) -> tuple[list[int], list[int]]:
    first, last = [], []
    at = 0
    for line in old:
        while at < len(new) and new[at] != line:
            at += 1
        if at == len(new):
            raise ValueError("raw source changes are not insertion-only")
        first.append(at)
        at += 1
    at = len(new) - 1
    for line in reversed(old):
        while at >= 0 and new[at] != line:
            at -= 1
        if at < 0:
            raise ValueError("raw source changes are not insertion-only")
        last.append(at)
        at -= 1
    return first, list(reversed(last))


def _raw_projection(old: str, new: str, points: set[int]) -> dict[int, int]:
    from bisect import bisect_right
    from itertools import accumulate

    before, after = old.splitlines(keepends=True), new.splitlines(keepends=True)
    first, last = _line_embeddings(before, after)
    starts = [0, *accumulate(map(len, before))]
    targets = [0, *accumulate(map(len, after))]
    projected = {}
    for point in points:
        index = bisect_right(starts, point) - 1
        boundary = point in starts
        neighbours = (
            [i for i in (index - 1, index) if 0 <= i < len(before)] if boundary else [index]
        )
        if not neighbours or any(first[i] != last[i] for i in neighbours):
            raise ValueError("raw source endpoint has ambiguous insertion origins")
        if boundary:
            left = targets[first[index - 1] + 1] if index else 0
            right = targets[first[index]] if index < len(before) else len(new)
            if left != right:
                raise ValueError("raw insertion lies at an original unit or separator boundary")
            projected[point] = left
        else:
            projected[point] = targets[first[index]] + point - starts[index]
    return projected


def counterpart_ranges(historical, current, units, sources: SourceOrigins):
    """Compute total counterpart cuts from unique raw origins, never supplied offsets.

    Callers still authenticate raw Git bytes, locale and original paid request evidence.
    The range proof does not admit calls or certify any translated reply.
    """
    if any(unit.reply_ref is None for unit in units):
        raise ValueError("source origin correspondence requires completed original units")
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
    raw = _raw_projection(sources.historical_raw, sources.current_raw, {inverse[p] for p in cuts})
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
