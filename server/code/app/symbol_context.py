"""Several definitions, ranked by the change at hand.

A bare name on a large repository resolves to many definitions (`DateTime`,
`executeImpl`, `listFiles`: ten each on the ClickHouse index) and the
default order — source before test paths, compiler tier first, types over
functions, then span — knows nothing about which one the caller is looking
at. `code.symbol(near_paths=[...])` or `code.symbol(pr_number=N)` supplies
that context: the files of the change under review. The definitions are
ranked, never filtered, by

1. `same_file`     — the definition's file is one of the near paths;
2. `same_dir`      — its directory is a near path's directory;
3. `co_change`     — merged PRs that touched it together with a near path
                     (`code_pr_history`, read only when 1 and 2 tie at the top);
4. `path_distance` — path components not shared with the nearest near path.

Ties keep the default order. `picked_by` names the first key on which rank
1 beat rank 2 — the context that decided — or `none` when nothing separated
them (or when there was only one definition to begin with).
"""

from __future__ import annotations

import logging
import posixpath
from typing import Any

from server.core.relative_paths import without_dot_slashes

logger = logging.getLogger("loom.code_symbol_context")

PICKED_BY = ("same_file", "same_dir", "co_change", "path_distance")
NONE = "none"
MAX_NEAR_PATHS = 50


def normalize_path(p: str) -> str:
    p = str(p or "").strip()
    p = without_dot_slashes(p)
    return p.lstrip("/")


def path_distance(a: str, b: str) -> int:
    """Path components not shared between `a` and `b`: 0 for the same file,
    2 for two files in one directory, more as the paths diverge."""
    pa, pb = normalize_path(a).split("/"), normalize_path(b).split("/")
    common = 0
    for x, y in zip(pa, pb, strict=False):
        if x != y:
            break
        common += 1
    return len(pa) + len(pb) - 2 * common


def _keys(
    row: dict[str, Any], near: list[str], co_change: dict[str, int] | None
) -> tuple[int, int, int, int]:
    path = normalize_path(str(row.get("path") or ""))
    same_file = 0 if path in near else 1
    dirs = {posixpath.dirname(n) for n in near}
    same_dir = 0 if posixpath.dirname(path) in dirs else 1
    co = -(co_change or {}).get(path, 0)
    dist = min((path_distance(path, n) for n in near), default=0)
    return (same_file, same_dir, co, dist)


def top_two_tie_before_co_change(rows: list[dict[str, Any]], near: list[str]) -> bool:
    """Whether the co-change read is needed: the best two definitions agree
    on `same_file` and `same_dir`, so only history or distance can order
    them. A ranking decided by the first two keys reads no table."""
    if len(rows) < 2:
        return False
    keyed = sorted(_keys(r, near, None)[:2] for r in rows)
    return keyed[0] == keyed[1]


def rank_definitions(
    rows: list[dict[str, Any]], near_paths: list[str], co_change: dict[str, int] | None = None
) -> tuple[list[dict[str, Any]], str]:
    """(rows in context order, picked_by). Stable: rows that tie on every key
    keep the order they arrived in. Every row stays in the list."""
    near = [normalize_path(p) for p in near_paths if normalize_path(p)][:MAX_NEAR_PATHS]
    if not near or len(rows) < 2:
        return list(rows), NONE
    keyed = [(_keys(r, near, co_change), i, r) for i, r in enumerate(rows)]
    keyed.sort(key=lambda t: (t[0], t[1]))
    first, second = keyed[0][0], keyed[1][0]
    picked_by = NONE
    for name, a, b in zip(PICKED_BY, first, second, strict=True):
        if a != b:
            picked_by = name
            break
    return [t[2] for t in keyed], picked_by


async def context_paths(
    repo: Any, org: str, ns: str, *, near_paths: list[str], pr_number: int
) -> list[str]:
    """The near paths as given, else the merged PR's file list from the
    ledger (`code_pr_history`). An unknown PR — an issue number, an unmerged
    PR, one outside the backfill — yields no context and the default order
    stands. Never raises."""
    if near_paths:
        return [normalize_path(p) for p in near_paths if normalize_path(p)][:MAX_NEAR_PATHS]
    if not pr_number:
        return []
    try:
        facts = await repo.prs_by_number(org, ns, [int(pr_number)])
    except Exception as e:  # noqa: BLE001 — context is a ranking aid, never fatal
        logger.debug("code.symbol pr_number context read failed: %s", e)
        return []
    row = facts.get(int(pr_number)) or {}
    files = [normalize_path(str(p)) for p in (row.get("files") or []) if str(p)]
    return files[:MAX_NEAR_PATHS]


async def rank_by_context(
    repo: Any,
    org: str,
    ns: str,
    rows: list[dict[str, Any]],
    *,
    near_paths: list[str],
    pr_number: int,
) -> tuple[list[dict[str, Any]], str]:
    """The handler's one call: resolve the context, read co-change only when
    the first two keys leave the top two tied, rank. Returns (rows,
    picked_by); `picked_by` is `none` when no context could be resolved."""
    near = await context_paths(repo, org, ns, near_paths=near_paths, pr_number=pr_number)
    if not near or len(rows) < 2:
        return list(rows), NONE
    co_change: dict[str, int] = {}
    if top_two_tie_before_co_change(rows, near):
        try:
            co_change = await repo.co_change_counts(
                org,
                ns,
                def_paths=[normalize_path(str(r.get("path") or "")) for r in rows],
                near_paths=near,
            )
        except Exception as e:  # noqa: BLE001 — falls back to path distance
            logger.debug("code.symbol co-change read failed: %s", e)
            co_change = {}
    return rank_definitions(rows, near, co_change)
