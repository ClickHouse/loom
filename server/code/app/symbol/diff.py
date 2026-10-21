"""Unified-diff parsing for the diff-shaped ops (code.impact, code.test_gate): the changed
line ranges on the new side and the hunk context names, and the touched function rows
picked by name match and span overlap.
"""

from __future__ import annotations

import re
from typing import Any


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


def _parse_unified_diff_full(
    diff: str,
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, set[str]]]:
    """(ranges, header_names): the NEW-side line ranges per path, plus the function names
    git put in each hunk header (`@@ … @@ <context>`). The names are drift-proof: hunk
    line numbers are as of the PR's commit and stop matching once master moves, but the
    enclosing function's name does not, so seeds match by name first and by span second."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    names: dict[str, set[str]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            path = "" if p == "/dev/null" else p
            if path:
                ranges.setdefault(path, [])
                names.setdefault(path, set())
        elif path and line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@(.*)$", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or 1)
                ranges[path].append((start, max(start, start + count - 1)))
                nm = _hunk_context_name(m.group(3) or "")
                if nm:
                    names[path].add(nm)
    return ranges, names


def _name_matches(qualified: str, header_name: str) -> bool:
    """`BaseSettings::write` matches `DB::BaseSettings::write`; a bare
    `maskPresignedURLParameters` matches its qualified form; never the
    reverse (a bare header name must not match every `x::write`)."""
    if not header_name:
        return False
    if qualified == header_name or qualified.endswith("::" + header_name):
        return True
    return False


def _touched_rows_for_diff(
    symbol_rows: list[dict[str, Any]],
    hunks: dict[str, list[tuple[int, int]]],
    header_names: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """(the touched function rows in seed order, the ids the walk seeds). Narrows the
    functions of the changed files to the ones the hunks edit when hunk ranges are known;
    a file without hunks (files-only shape) touches every function in it. Pure, so the
    diff shape's seed rule is testable without a repo."""
    touched_rows: list[dict[str, Any]] = []
    for r in symbol_rows:
        p = str(r["path"])
        spans = hunks.get(p)
        names = header_names.get(p) or set()
        via = "file"
        if spans:
            # Name first (drift-proof: hunk lines are as of the PR's commit, symbols are as
            # of master), span overlap second.
            if any(_name_matches(str(r["qualified_name"]), n) for n in names):
                via = "header"
            elif any(
                s <= int(r["end_line"] or 0) and e >= int(r["start_line"] or 0) for s, e in spans
            ):
                via = "hunk"
            else:
                continue  # file is in the diff but this function is not
        touched_rows.append({**r, "via": via})
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
    # Deterministic, explainable order: header-named symbols first (the function git
    # itself says the hunk sits in), then span hits, then whole-file seeds.
    rank = {"header": 0, "hunk": 1, "file": 2}
    rows = sorted(
        kept.values(), key=lambda r: (rank[r["via"]], str(r["path"]), int(r["start_line"] or 0))
    )
    return rows, list(dict.fromkeys(sid for r in rows for sid in r["tier_ids"]))
