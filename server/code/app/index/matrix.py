"""Maintenance matrix from merged-PR history: "when this file changes, these
files usually change with it" — computed, with evidence, instead of written
from memory into an instructions file.

An agent would otherwise hand-author this table into `.github/copilot-instructions.md`.
The PR ledger holds every merged PR's file list, so the table is a query: over the last
`prs` merged PRs with 2–30 files, count (a, b) co-occurrences,
divide by a's own PR count for a directional confidence, keep pairs above both floors.
Header/implementation pairs (`X.h` / `X.cpp`) are dropped — nobody forgets those; the
matrix is for the partner in another directory that follow-up PRs forget (e.g.
SettingsChangesHistory.cpp for Settings.cpp, Context.cpp for SettingsConstraints.cpp).

`render_matrix` is pure so the table shape is unit-tested; `co_change_pairs`
runs the one ClickHouse query (~1 s on 3,000 PRs).
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

DEFAULT_PRS = 3000
DEFAULT_MIN_TOGETHER = 4
DEFAULT_MIN_CONF = 0.5
_SKIP_PREFIXES = ("tests/", "docs/", "utils/check-style/")
_SKIP_SUFFIXES = (".md", ".reference", ".txt", ".json", ".yml", ".yaml")


@dataclass(frozen=True)
class Pair:
    a: str  # when this changes …
    b: str  # … this usually changes too
    together: int  # PRs touching both
    support_a: int  # PRs touching a
    conf: float  # together / support_a


def _stem(path: str) -> str:
    return posixpath.basename(path).rsplit(".", 1)[0]


def is_trivial_pair(a: str, b: str) -> bool:
    """Same directory and same stem: the header of the implementation."""
    return posixpath.dirname(a) == posixpath.dirname(b) and _stem(a) == _stem(b)


def _skip(path: str) -> bool:
    return path.startswith(_SKIP_PREFIXES) or path.endswith(_SKIP_SUFFIXES)


async def co_change_pairs(
    repo: Any,
    org: str,
    ns: str,
    *,
    prs: int = DEFAULT_PRS,
    min_together: int = DEFAULT_MIN_TOGETHER,
    min_conf: float = DEFAULT_MIN_CONF,
    limit: int = 2000,
) -> list[Pair]:
    rows = await repo.co_change_rows(
        org, ns, prs=prs, min_together=min_together, min_conf=min_conf, limit=limit
    )
    return filter_pairs(
        [
            Pair(
                str(r["a"]), str(r["b"]), int(r["together"]), int(r["support_a"]), float(r["conf"])
            )
            for r in rows
        ]
    )


def filter_pairs(pairs: list[Pair]) -> list[Pair]:
    return [p for p in pairs if not is_trivial_pair(p.a, p.b) and not _skip(p.a) and not _skip(p.b)]


DEFAULT_MAX_ROWS = 40


def render_matrix(
    pairs: list[Pair],
    *,
    prs: int,
    head: str = "",
    max_partners: int = 6,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> str:
    """Markdown table grouped by source file, most-evidenced first. Each
    partner carries its evidence inline so a reader (or an agent) can weigh
    it; the header names the window so the table dates itself.

    Capped at `max_rows` sources: this lands in an instructions file that
    every session loads, and the full ClickHouse matrix (180 sources,
    ~10k tokens) would cost more per session than the misses it prevents.
    Forty rows is ~2.3k tokens and still covers every hub file."""
    by_a: dict[str, list[Pair]] = defaultdict(list)
    for p in pairs:
        by_a[p.a].append(p)
    order = sorted(by_a, key=lambda a: (-max(p.together for p in by_a[a]), a))
    omitted = max(0, len(order) - max_rows) if max_rows > 0 else 0
    if max_rows > 0:
        order = order[:max_rows]
    lines = [
        "## Maintenance matrix (computed from merged-PR history)",
        "",
        f"Over the last {prs:,} merged PRs"
        + (f" as of {head[:12]}" if head else "")
        + ": when a file in the first column changes, the files in the second "
        "usually change in the same PR. Evidence = PRs touching both / PRs "
        "touching the first. Statistical lead, not a rule.",
        "",
        "| When this changes… | …also update | Evidence |",
        "|---|---|---|",
    ]
    for a in order:
        partners = sorted(by_a[a], key=lambda p: (-p.together, p.b))[:max_partners]
        cells = "<br>".join(f"`{p.b}`" for p in partners)
        ev = "<br>".join(f"{p.together}/{p.support_a} ({p.conf:.0%})" for p in partners)
        lines.append(f"| `{a}` | {cells} | {ev} |")
    if not order:
        lines.append("| _no pair above the floors_ | | |")
    if omitted:
        lines += [
            "",
            f"_{omitted} more source files below the row cap; "
            "`loom code export-matrix --max-rows 0` for all._",
        ]
    return "\n".join(lines) + "\n"
