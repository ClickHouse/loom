"""Unified-diff parsing for the diff-shaped ops (code.impact, code.test_gate): the changed
lines on the side the index holds, the new-side ranges and the hunk context names, and the
touched definitions those lines sit in.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from typing import Any, NamedTuple

# What a diff's changed lines are matched against. A class is a definition a diff can edit
# outside every method (an attribute, a decorator), and a line of its body is not the
# method's below it.
DEFINITION_KINDS = ("function", "method", "fixture", "class")


class Hunk(NamedTuple):
    """What one hunk changes on the old side, the revision the index holds: a diff of a
    working tree starts from HEAD, a pull request's from its merge base."""

    # The function git named in the hunk header, '' when it named none.
    name: str
    # Old-side line ranges the hunk deletes or replaces, inclusive.
    removed: tuple[tuple[int, int], ...]
    # For each run of added lines that replaces nothing, the old line it follows (0 at the
    # top of the file).
    inserted: tuple[int, ...]


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
                inserted = [old_start]
        changes[path].append(Hunk(nm, tuple(removed), tuple(inserted)))
    return ranges, names, changes


def _hunk_body(
    lines: list[str], i: int, old_start: int, old_left: int, new_left: int
) -> tuple[list[tuple[int, int]], list[int], int]:
    """(removed, inserted, next index): the old-side changes of the hunk body starting at
    `lines[i]`, read for as long as the header's line counts last. A run of added lines
    that follows removed lines replaces them and is no insertion of its own."""
    removed: list[tuple[int, int]] = []
    inserted: list[int] = []
    # A zero-count old range names the line the hunk follows, any other its first line.
    old = old_start if old_left else old_start + 1
    in_run = False  # inside a run of changed lines that already removed or inserted
    seen = False
    while i < len(lines) and (old_left > 0 or new_left > 0):
        line = lines[i]
        if line.startswith("\\"):  # "\ No newline at end of file"
            i += 1
            continue
        # The next file's header, when the hunk came without its lines.
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not seen and line.startswith("--- ") and nxt.startswith("+++ "):
            break
        if line.startswith("-") and old_left > 0:
            if in_run and removed and removed[-1][1] == old - 1:
                removed[-1] = (removed[-1][0], old)
            else:
                removed.append((old, old))
            old, old_left, in_run = old + 1, old_left - 1, True
        elif line.startswith("+") and new_left > 0:
            if not in_run:
                inserted.append(old - 1)
            new_left, in_run = new_left - 1, True
        elif (line.startswith(" ") or not line) and old_left > 0 and new_left > 0:
            old, old_left, new_left, in_run = old + 1, old_left - 1, new_left - 1, False
        else:
            break
        seen = True
        i += 1
    return removed, inserted, i


def _name_matches(qualified: str, header_name: str) -> bool:
    """`BaseSettings::write` matches `DB::BaseSettings::write`; a bare
    `maskPresignedURLParameters` matches its qualified form; never the
    reverse (a bare header name must not match every `x::write`)."""
    if not header_name:
        return False
    if qualified == header_name or qualified.endswith("::" + header_name):
        return True
    return False


def _span(r: dict[str, Any]) -> tuple[int, int]:
    start = int(r["start_line"] or 0)
    return start, max(start, int(r["end_line"] or 0))


def _hunk_rows(rows: list[dict[str, Any]], hunk: Hunk) -> tuple[list[dict[str, Any]], int]:
    """(the rows of one file the hunk edits, its changed lines that sit in none of them).
    The edited definition is the innermost one whose span holds a changed line. An
    insertion sits between two old lines and is inside a definition that holds both, or
    directly above one (a decorator). `rows` is ordered widest span first."""
    out: dict[str, dict[str, Any]] = {}
    unmapped = 0
    lo = min((_span(r)[0] for r in rows), default=1)
    hi = max((_span(r)[1] for r in rows), default=0)
    lines: list[int] = []
    for a, b in hunk.removed:
        # Lines outside every definition of the file need no walk: a header may claim any range.
        inside = range(max(a, lo), min(b, hi) + 1)
        lines.extend(inside)
        unmapped += (b - a + 1) - len(inside)
    lines = sorted(set(lines))
    owner: dict[int, dict[str, Any]] = {}
    for r in rows:  # widest first, so the innermost definition is written last
        start, end = _span(r)
        for n in lines[bisect_left(lines, start) : bisect_right(lines, end)]:
            owner[n] = r
    for n in lines:
        if n in owner:
            out[str(owner[n]["symbol_id"])] = owner[n]
        else:
            unmapped += 1
    for after in hunk.inserted:
        holders = [r for r in rows if _span(r)[0] <= after and after + 1 <= _span(r)[1]]
        row = (holders or [r for r in rows if _span(r)[0] == after + 1])[-1:]
        if row:
            out[str(row[0]["symbol_id"])] = row[0]
        else:
            unmapped += 1
    return list(out.values()), unmapped


def _header_row(rows: list[dict[str, Any]], hunk: Hunk) -> dict[str, Any] | None:
    """The definition a hunk's header names, for a hunk whose lines sit in none: the
    nearest same-named row above the hunk, and only one the index holds no body extent
    for (a compiler row of one line), where line numbers cannot tell."""
    first = min([a for a, _ in hunk.removed] + list(hunk.inserted), default=0)
    above = [
        r
        for r in rows
        if _name_matches(str(r["qualified_name"]), hunk.name) and _span(r)[0] <= first
    ]
    if not above:
        return None
    row = max(above, key=lambda r: _span(r)[0])
    return row if _span(row)[0] == _span(row)[1] else None


def _touched_rows_for_diff(
    symbol_rows: list[dict[str, Any]],
    changes: dict[str, list[Hunk]],
) -> tuple[list[dict[str, Any]], list[str], int]:
    """(the touched definition rows in seed order, the ids the walk seeds, the changed
    lines of indexed files that sit in no definition). A file with hunks touches the
    definitions its changed lines sit in; a file without hunks (files-only shape) touches
    every function in it. Pure, so the diff shape's seed rule is testable without a repo."""
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
        rows = sorted(rows, key=lambda r: _span(r)[0] - _span(r)[1])
        edited: set[str] = set()
        for hunk in hunks:
            found, missed = _hunk_rows(rows, hunk)
            named = _header_row(rows, hunk) if not found and hunk.name else None
            edited |= {str(r["qualified_name"]) for r in found}
            if named is not None:
                touched_rows.append({**named, "via": "header"})
            else:
                unmapped += missed
        # A definition the two tiers hold as two rows is edited in both, whichever row's
        # span held the line (the compiler's may be its declaration line only).
        touched_rows += [{**r, "via": "hunk"} for r in rows if str(r["qualified_name"]) in edited]
    # Both tiers index the same definition under different symbol_ids: one row per (path,
    # qualified_name), scip first, carrying every id of the rows it stands for (`tier_ids`).
    # The walk seeds all of them: parser-tier edges point at the parser-tier id, and a
    # definition seeded by its compiler id alone would lose the callers only the parser saw.
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    for r in sorted(touched_rows, key=lambda r: 0 if str(r["tier"]) == "scip" else 1):
        key = (str(r["path"]), str(r["qualified_name"]))
        ids = r.get("tier_ids") or [r["symbol_id"]]
        if key in kept:
            first = kept[key]
            first["tier_ids"] = list(dict.fromkeys([*first["tier_ids"], *map(str, ids)]))
        else:
            kept[key] = {**r, "tier_ids": list(dict.fromkeys(map(str, ids)))}
    # Deterministic, explainable order: the definitions a changed line sits in, then the
    # ones only a hunk header names, then whole-file seeds.
    rank = {"hunk": 0, "header": 1, "file": 2}
    out = sorted(
        kept.values(), key=lambda r: (rank[r["via"]], str(r["path"]), int(r["start_line"] or 0))
    )
    return out, list(dict.fromkeys(sid for r in out for sid in r["tier_ids"])), unmapped
