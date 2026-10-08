"""Symbol lookup shared by every structural op: a name, a symbol id or a
`path:line` to definition rows, the near misses of a name that resolved nothing, and
the chunk body and file row of a definition.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from server.code.app.common import repo_relative
from server.code.app.index import symbol_forms
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)


async def _resolve_symbols(
    repo: CodeRepo, org: str, ns: str, *, symbol_id: str, name: str
) -> list[dict[str, Any]]:
    if symbol_id:
        return await repo.symbols_by_ids(org, ns, [symbol_id])
    loc = _parse_location(name)
    if loc:
        # `path:line` — the innermost function containing the line, the
        # shape a grep hit or a review comment arrives in.
        return await _symbols_at_location(repo, org, ns, *loc)
    if name:
        rows = await repo.symbols_by_name(org, ns, name)
        if rows:
            return rows
        # An over-qualified form (`(anonymous namespace)::` segments, a namespace the
        # definition does not carry) binds when exactly one definition fits; the rows
        # carry `resolved_by`.
        rows, resolved_by = await symbol_forms.resolve_tolerant(repo, org, ns, name)
        return symbol_forms.mark(rows, resolved_by)
    return []


def _parse_location(name: str) -> tuple[str, int] | None:
    """`src/Foo.cpp:123` -> ("src/Foo.cpp", 123); None for a symbol name.
    Requires a file extension so `ns::f` and `Foo:12`-style names never
    parse as locations."""
    m = _FILE_LINE.match(repo_relative(name))
    if not m:
        return None
    return m.group("path"), int(m.group("line"))


def _containing(rows: list[dict[str, Any]], line: int) -> list[dict[str, Any]]:
    """The rows whose span contains `line`, tightest first (a lambda's enclosing function
    beats the file-level one); ties keep their input order. A single-line span (scip) only
    contains its own line."""
    inside = [
        r for r in rows if int(r.get("start_line") or 0) <= line <= int(r.get("end_line") or 0)
    ]
    return sorted(inside, key=lambda r: int(r.get("end_line") or 0) - int(r.get("start_line") or 0))


def _innermost(rows: list[dict[str, Any]], line: int) -> dict[str, Any] | None:
    """The tightest span containing `line`, None when none does."""
    inside = _containing(rows, line)
    return inside[0] if inside else None


async def _symbols_at_location(
    repo: CodeRepo, org: str, ns: str, path: str, line: int
) -> list[dict[str, Any]]:
    """Innermost function span containing `path:line` (a lambda's enclosing
    function, not the file), plus the compiler row for the same name when
    the span came from tree-sitter: scip-clang emits single-line spans, so
    line containment finds the tree-sitter row, and the scip row is what
    carries edges and the tier the caller wants to see."""
    rows = await repo.functions_for_paths(org, ns, [path], limit=2000)
    picked = _containing(rows, line)[:2]
    out: list[dict[str, Any]] = list(picked)
    seen = {str(r["symbol_id"]) for r in out}
    for r in picked:
        for s in await repo.symbols_by_name(org, ns, str(r["qualified_name"]), limit=4):
            if str(s["symbol_id"]) not in seen:
                seen.add(str(s["symbol_id"]))
                out.append(s)
    return out


async def _functions_near(
    repo: CodeRepo, org: str, ns: str, path: str, line: int, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Functions in `path` ordered by distance to `line` — the candidates
    for a path:line lookup that landed at file scope."""
    rows = await repo.functions_for_paths(org, ns, [path], limit=2000)

    def dist(r: dict[str, Any]) -> int:
        lo, hi = int(r.get("start_line") or 0), int(r.get("end_line") or 0)
        return 0 if lo <= line <= hi else min(abs(line - lo), abs(line - hi))

    return sorted(rows, key=dist)[:limit]


async def near_misses(
    repo: CodeRepo, org: str, ns: str, name: str, *, fallback: str = "", limit: int = 10
) -> list[dict[str, Any]]:
    """The near-miss list for a name that resolved nothing: symbols whose name contains
    its bare name, or for a `path:line` the functions in that file nearest the line."""
    loc = _parse_location(name)
    if loc:
        fuzzy = await _functions_near(repo, org, ns, *loc)
    else:
        fuzzy = await repo.symbols_fuzzy(org, ns, name.split("::")[-1] or fallback)
    return [
        {
            "qualified_name": str(r["qualified_name"]),
            "path": str(r["path"]),
            "kind": str(r.get("kind") or "function"),
            "tier": str(r["tier"]),
            "start_line": int(r.get("start_line") or 0),
            "end_line": int(r.get("end_line") or 0),
        }
        for r in fuzzy[:limit]
    ]


async def symbol_body(repo: CodeRepo, org: str, ns: str, sym: dict[str, Any]) -> str:
    """The chunk content covering the symbol's definition (whole unit)."""
    path = str(sym["path"])
    bodies = await repo.chunk_bodies_with_symbol(org, ns, path, str(sym["symbol_id"]))
    if not bodies:
        # Unchanged chunks keep their original symbol_ids array (they are
        # content-addressed and never rewritten), so after a symbol-id
        # normalization change the array can be stale — fall back to the
        # innermost chunk covering the symbol's first line.
        inner = await repo.innermost_chunk_at(org, ns, path, int(sym.get("start_line") or 0))
        bodies = [inner] if inner is not None else []
    return "\n".join(bodies)[:12000]


async def _file_row(repo: CodeRepo, org: str, ns: str, path: str) -> dict[str, Any] | None:
    """The live `code_files` row for `path`, None when the index holds none. Fail-open to
    None: a failed read may only lower a verdict (to `not_covered`), never raise it."""
    try:
        rows = await repo.live_files_for_path(org, ns, path)
    except Exception as e:  # noqa: BLE001
        logger.debug("file-row read skipped: %s", e)
        return None
    return rows[0] if rows else None


def _seed_ids_of(seeds: list[dict[str, Any]]) -> list[str]:
    """A definition both tiers hold seeds the walk under BOTH ids (`tier_ids`, carried by
    `_prefer_scip`'s collapse): the parser's edges point at the parser id, the compiler's
    at its own."""
    return list(
        dict.fromkeys(str(sid) for r in seeds for sid in (r.get("tier_ids") or [r["symbol_id"]]))
    )


_FILE_LINE = re.compile(r"^(?P<path>[\w./+-]+\.[A-Za-z0-9]+):(?P<line>\d+)$")
