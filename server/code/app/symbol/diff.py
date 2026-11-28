"""Unified-diff parsing for the diff-shaped ops (code.impact, code.test_gate): the changed
lines on the side the index holds, the new-side ranges and the hunk context names, and the
touched definitions those lines sit in.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from typing import Any, NamedTuple

from server.code.domain.extract.treesitter import lang_for_path
from server.code.domain.index import coverage_runs as cr

# What a diff's changed lines are matched against. A class is a definition a diff can edit
# outside every method (an attribute, a decorator), and a line of its body is not the
# method's below it.
DEFINITION_KINDS = ("function", "method", "fixture", "class")


class Insertion(NamedTuple):
    """A run of added lines that replaces nothing. The two flags are read from the added
    text, so a hunk sent without its lines leaves both False."""

    # The old line the run follows (0 at the top of the file).
    after: int
    # Every added line is a decorator or annotation (`@name`): the run decorates the
    # definition that starts on the next line. A new function added there has other lines.
    decorates: bool = False
    # The first added line is indented at least as deep as the old line above it: where
    # indentation is the block structure, the run continues the block that line closes.
    continues: bool = False


class Hunk(NamedTuple):
    """What one hunk changes on the old side, the revision the index holds: a diff of a
    working tree starts from HEAD, a pull request's from its merge base."""

    # The function git named in the hunk header, '' when it named none.
    name: str
    # Old-side line ranges the hunk deletes or replaces, inclusive.
    removed: tuple[tuple[int, int], ...]
    # The runs of added lines that replace nothing.
    inserted: tuple[Insertion, ...]


def _hunk_context_name(ctx: str) -> str:
    """Function name from a hunk header's context (`@@ … @@ void
    BaseSettings<TTraits>::write(WriteBuffer & out)` → `BaseSettings::write`).
    Empty for non-function context (`public:`, a comment, a bare `try`). The coding-agent
    hooks compute this name, or none where the context is value-like or credential-shaped,
    before sending a diff (`hunkFunctionName` in
    integrations/claude-code-plugin/lib/loom-client.mjs; tests/unit/test_hook_diff_headers.py
    holds the two together)."""
    ctx = ctx.strip()
    if not ctx or "(" not in ctx:
        return ""
    head: str = ctx.split("(", 1)[0].strip()
    # drop template arguments (two passes for one level of nesting), keep scope operators
    head = re.sub(r"<[^<>]*>", "", head)
    head = re.sub(r"<[^<>]*>", "", head)
    tokens = head.split()
    if not tokens:
        return ""
    cand: str = tokens[-1].strip("&*")
    if cand in {"if", "for", "while", "switch", "return", "catch", "sizeof", "decltype"}:
        return ""
    if not re.fullmatch(r"~?[A-Za-z_]\w*(::~?[A-Za-z_]\w*)*", cand):
        return ""
    return cand


_HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def _parse_unified_diff_full(
    diff: str,
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, set[str]], dict[str, list[Hunk]]]:
    """(ranges, header_names, changes): per path, the NEW-side line ranges, the function
    names git put in the hunk headers (`@@ … @@ <context>`), and each hunk's OLD-side
    changed lines. A hunk sent without its lines (the coding-agent hooks send headers
    only, from a zero-context diff) changes what its header's old range says: `-12,3`
    replaces lines 12 to 14, `-12,0` inserts after line 12. With context lines in a
    headers-only diff the range includes them."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    names: dict[str, set[str]] = {}
    changes: dict[str, list[Hunk]] = {}
    lines = diff.splitlines()
    path = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            path = "" if p == "/dev/null" else p
            if path:
                ranges.setdefault(path, [])
                names.setdefault(path, set())
                changes.setdefault(path, [])
            continue
        m = _HUNK_RE.match(line) if path and line.startswith("@@") else None
        if not m:
            continue
        old_start, old_count = int(m.group(1)), int(m.group(2) or 1)
        start, count = int(m.group(3)), int(m.group(4) or 1)
        ranges[path].append((start, max(start, start + count - 1)))
        nm = _hunk_context_name(m.group(5) or "")
        if nm:
            names[path].add(nm)
        removed, inserted, i = _hunk_body(lines, i, old_start, old_count, count)
        if not removed and not inserted:
            if old_count:
                removed = [(old_start, old_start + old_count - 1)]
            else:
                inserted = [Insertion(old_start)]
        changes[path].append(Hunk(nm, tuple(removed), tuple(inserted)))
    return ranges, names, changes


def _file_header_at(lines: list[str], i: int, *, body_seen: bool) -> bool:
    """Whether `lines[i]` opens the next file's header where a hunk's counts still expect
    lines: the hunk came without its lines, or with fewer than its header counts. A
    `--- ` and `+++ ` pair inside a body is a removed and an added line unless a hunk
    header (or the end of the diff) follows it."""
    line = lines[i]
    after = lines[i + 1 : i + 3] + ["@@", "@@"]  # the end of the diff reads as a header
    if line.startswith("+++ "):
        return not body_seen or after[0].startswith("@@")
    if line.startswith("--- ") and after[0].startswith("+++ "):
        return not body_seen or after[1].startswith("@@")
    return False


_DECORATOR_RE = re.compile(r"\s*@\w")


def _indent(text: str) -> int:
    text = text.expandtabs()
    return len(text) - len(text.lstrip())


def _insertion(after: int, added: list[str], above: str | None) -> Insertion:
    """The insertion of `added` after old line `after`, whose text is `above` when the
    hunk showed it."""
    lines = [a for a in added if a.strip()]
    if not lines:
        return Insertion(after)
    return Insertion(
        after,
        decorates=all(_DECORATOR_RE.match(a) for a in lines),
        continues=bool(above and above.strip()) and _indent(lines[0]) >= _indent(above or ""),
    )


def _hunk_body(
    lines: list[str], i: int, old_start: int, old_left: int, new_left: int
) -> tuple[list[tuple[int, int]], list[Insertion], int]:
    """(removed, inserted, next index): the old-side changes of the hunk body starting at
    `lines[i]`, read for as long as the header's line counts last. A run of added lines
    that follows removed lines replaces them and is no insertion of its own."""
    removed: list[tuple[int, int]] = []
    runs: list[tuple[int, list[str], str | None]] = []  # (after, added lines, the line above)
    # A zero-count old range names the line the hunk follows, any other its first line.
    old = old_start if old_left else old_start + 1
    in_run = False  # inside a run of changed lines that already removed or inserted
    seen = False
    above: str | None = None  # the old line before the next one, when the hunk showed it
    adding: list[str] | None = None  # the added lines of an insertion still being read
    while i < len(lines) and (old_left > 0 or new_left > 0):
        line = lines[i]
        if line.startswith("\\"):  # "\ No newline at end of file"
            i += 1
            continue
        if _file_header_at(lines, i, body_seen=seen):
            break
        if line.startswith("-") and old_left > 0:
            if in_run and removed and removed[-1][1] == old - 1:
                removed[-1] = (removed[-1][0], old)
            else:
                removed.append((old, old))
            above, adding = line[1:], None
            old, old_left, in_run = old + 1, old_left - 1, True
        elif line.startswith("+") and new_left > 0:
            if not in_run:
                adding = []
                runs.append((old - 1, adding, above))
            if adding is not None:
                adding.append(line[1:])
            new_left, in_run = new_left - 1, True
        elif (line.startswith(" ") or not line) and old_left > 0 and new_left > 0:
            above, adding = line[1:], None
            old, old_left, new_left, in_run = old + 1, old_left - 1, new_left - 1, False
        else:
            break
        seen = True
        i += 1
    return removed, [_insertion(*r) for r in runs], i


def _name_matches(qualified: str, header_name: str) -> bool:
    """`BaseSettings::write` matches `DB::BaseSettings::write`; a bare
    `maskPresignedURLParameters` matches its qualified form; never the
    reverse (a bare header name must not match every `x::write`)."""
    if not header_name:
        return False
    if qualified == header_name or qualified.endswith("::" + header_name):
        return True
    return False


def _bare(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def _span(r: dict[str, Any]) -> tuple[int, int]:
    start = int(r["start_line"] or 0)
    return start, max(start, int(r["end_line"] or 0))


def _clip(ranges: list[cr.Range], within: list[cr.Range]) -> list[cr.Range]:
    """The parts of `ranges` inside `within`; both sorted and disjoint."""
    out: list[cr.Range] = []
    j = 0
    for a, b in ranges:
        while j < len(within) and within[j][1] < a:
            j += 1
        k = j
        while k < len(within) and within[k][0] <= b:
            out.append((max(a, within[k][0]), min(b, within[k][1])))
            k += 1
    return out


def _innermost(
    rows: list[dict[str, Any]], points: list[int], *, gap: bool = False
) -> dict[int, dict[str, Any]]:
    """The innermost row whose span holds each of `points` (sorted); with `gap`, the
    innermost that holds the point and the line after it. `rows` is ordered widest span
    first, so the innermost is written last."""
    owner: dict[int, dict[str, Any]] = {}
    for r in rows:
        start, end = _span(r)
        for n in points[bisect_left(points, start) : bisect_right(points, end - gap)]:
            owner[n] = r
    return owner


class _FileSpans:
    """The definition rows of one file, read for where a changed line sits."""

    def __init__(self, rows: list[dict[str, Any]], hunks: list[Hunk], *, indented: bool) -> None:
        # Widest span first: the innermost definition is the last one written.
        self.rows = sorted(rows, key=lambda r: _span(r)[0] - _span(r)[1])
        self.covered = cr.merge_ranges(_span(r) for r in self.rows)
        self.indented = indented
        self.starts = {_span(r)[0]: r for r in self.rows}
        self.ends = {_span(r)[1]: r for r in self.rows}
        gaps = sorted({ins.after for h in hunks for ins in h.inserted})
        self.holders = _innermost(self.rows, gaps, gap=True)
        # Rows by bare name in line order, with their start lines: a hunk header names a
        # definition, and the nearest one above the hunk is found by bisection.
        self.named: dict[str, tuple[list[int], list[dict[str, Any]]]] = {}
        for r in sorted(self.rows, key=lambda r: _span(r)[0]):
            starts, same = self.named.setdefault(_bare(str(r["qualified_name"])), ([], []))
            starts.append(_span(r)[0])
            same.append(r)

    def inserted_in(self, ins: Insertion) -> dict[str, Any] | None:
        """The definition an insertion edits. It sits between two old lines and is inside
        the innermost definition that holds both. The added text can say more: decorators
        edit the definition that starts below them, and in an indented language a run as
        deep as the line above continues the definition that line ends. Text added
        directly above a definition is otherwise a new one, which the index does not hold."""
        if ins.decorates and ins.after + 1 in self.starts:
            return self.starts[ins.after + 1]
        if self.indented and ins.continues and ins.after in self.ends:
            return self.ends[ins.after]
        return self.holders.get(ins.after)

    def holds(self, hunk: Hunk) -> bool:
        """Whether a changed line of the hunk sits in a definition."""
        for a, b in hunk.removed:
            i = bisect_left(self.covered, (a, 0))
            if (i < len(self.covered) and self.covered[i][0] <= b) or (
                i and self.covered[i - 1][1] >= a
            ):
                return True
        return any(self.inserted_in(ins) is not None for ins in hunk.inserted)

    def header_row(self, hunk: Hunk) -> dict[str, Any] | None:
        """The definition a hunk's header names, for a hunk whose lines sit in none: the
        nearest same-named row above the hunk, and only one the index holds no body extent
        for, where line numbers cannot tell."""
        first = min([a for a, _ in hunk.removed] + [ins.after for ins in hunk.inserted], default=0)
        starts, same = self.named.get(_bare(hunk.name), ([], []))
        for r in reversed(same[: bisect_right(starts, first)]):
            if _name_matches(str(r["qualified_name"]), hunk.name):
                return r if _span(r)[0] == _span(r)[1] else None
        return None

    def edited(self, hunks: list[Hunk]) -> tuple[list[dict[str, Any]], int]:
        """(the rows the hunks edit, their changed lines that sit in none). The edited
        definition is the innermost one whose span holds a changed line. The removed
        ranges are merged before the walk, so its cost follows the file's extent and not
        what the hunk headers claim."""
        removed = cr.merge_ranges(r for h in hunks for r in h.removed)
        inside = _clip(removed, self.covered)
        lines = [n for a, b in inside for n in range(a, b + 1)]
        out = {str(r["symbol_id"]): r for r in _innermost(self.rows, lines).values()}
        unmapped = cr.lines_in(removed) - len(lines)
        for ins in dict.fromkeys(ins for h in hunks for ins in h.inserted):
            row = self.inserted_in(ins)
            if row is None:
                unmapped += 1
            else:
                out[str(row["symbol_id"])] = row
        return list(out.values()), unmapped


def _touched_rows_for_diff(
    symbol_rows: list[dict[str, Any]],
    changes: dict[str, list[Hunk]],
) -> tuple[list[dict[str, Any]], list[str], int]:
    """(the touched definition rows in seed order, the ids the walk seeds, the changed
    lines that sit in no definition, counted in the files `symbol_rows` has a row of). A
    file with hunks touches the definitions its changed lines sit in; a file without hunks
    (files-only shape) touches every function in it. Pure, so the diff shape's seed rule
    is testable without a repo."""
    by_path: dict[str, list[dict[str, Any]]] = {}
    for r in symbol_rows:
        by_path.setdefault(str(r["path"]), []).append(r)
    touched_rows: list[dict[str, Any]] = []
    unmapped = 0
    for p, rows in by_path.items():
        hunks = changes.get(p)
        if not hunks:
            touched_rows += [{**r, "via": "file"} for r in rows if str(r.get("kind")) != "class"]
            continue
        spans = _FileSpans(rows, hunks, indented=lang_for_path(p) == "python")
        by_lines: list[Hunk] = []
        for hunk in hunks:
            named = spans.header_row(hunk) if hunk.name and not spans.holds(hunk) else None
            if named is not None:
                touched_rows.append({**named, "via": "header"})
            else:
                by_lines.append(hunk)
        found, missed = spans.edited(by_lines)
        unmapped += missed
        edited = {str(r["qualified_name"]) for r in found}
        # A definition the two tiers hold as two rows is edited in both, whichever row's
        # span held the line (the compiler's may be its declaration line only).
        touched_rows += [{**r, "via": "hunk"} for r in rows if str(r["qualified_name"]) in edited]
    # Both tiers index the same definition under different symbol_ids: one row per (path,
    # qualified_name), scip first, carrying every id of the rows it stands for (`tier_ids`)
    # and the span of each (`extents`). The walk seeds all the ids: parser-tier edges point
    # at the parser-tier id, and a definition seeded by its compiler id alone would lose the
    # callers only the parser saw. A coverage run is read against all the extents: the
    # compiler's may be the declaration line only.
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    for r in sorted(touched_rows, key=lambda r: 0 if str(r["tier"]) == "scip" else 1):
        key = (str(r["path"]), str(r["qualified_name"]))
        ids = r.get("tier_ids") or [r["symbol_id"]]
        extent = (str(r["symbol_id"]), *_span(r))
        if key in kept:
            first = kept[key]
            first["tier_ids"] = list(dict.fromkeys([*first["tier_ids"], *map(str, ids)]))
            first["extents"].append(extent)
        else:
            kept[key] = {**r, "tier_ids": list(dict.fromkeys(map(str, ids))), "extents": [extent]}
    # Deterministic, explainable order: the definitions a changed line sits in, then the
    # ones only a hunk header names, then whole-file seeds.
    rank = {"hunk": 0, "header": 1, "file": 2}
    out = sorted(
        kept.values(), key=lambda r: (rank[r["via"]], str(r["path"]), int(r["start_line"] or 0))
    )
    return out, list(dict.fromkeys(sid for r in out for sid in r["tier_ids"])), unmapped
