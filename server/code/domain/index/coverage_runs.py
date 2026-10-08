"""Pure logic of a measured-coverage import and of reading it back.

A per-test coverage run reports, per (source file, test), the line ranges the
test executed. Most executed lines are shared plumbing every test runs, so a
test-map row needs a *specific* line: one at most `cap` tests reach. The import
merges each pair's specific ranges, scores the pair by how few tests share its
most specific line, and maps the run's test name to the repository path of the
test file. The claim intersects those ranges with a subject's line span.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Where a run's test name lives in the repository: a stateless or stateful
# query test is named by its file, an integration test by its module path.
_STATELESS_DIRS = ("tests/queries/0_stateless/", "tests/queries/1_stateful/")
_INTEGRATION_DIR = "tests/integration/"

# Ranges kept in one row's evidence; a pair with more keeps its widest ones.
MAX_EVIDENCE_RANGES = 200

Range = tuple[int, int]


def merge_ranges(ranges: Iterable[Iterable[int]]) -> list[Range]:
    """Sorted, merged, inclusive line ranges; adjacent ranges join, an
    inverted or non-positive range is dropped."""
    pairs = ((int(t[0]), int(t[1])) for t in (tuple(r) for r in ranges))
    spans = sorted((a, b) for a, b in pairs if a > 0 and b >= a)
    out: list[Range] = []
    for a, b in spans:
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def intersects(ranges: Iterable[Range], start: int, end: int) -> bool:
    """True when any range shares a line with [start, end]."""
    return any(a <= end and start <= b for a, b in ranges)


def lines_in(ranges: Iterable[Range]) -> int:
    """How many lines the (merged) ranges cover."""
    return sum(b - a + 1 for a, b in ranges)


def specificity(min_tests: int) -> float:
    """A pair's confidence from the fewest tests that share one of its
    specific lines: 1.0 when only this test reaches the line, falling with the
    log of the count (0.3 at 200)."""
    return round(1.0 / (1.0 + math.log10(max(1, int(min_tests)))), 3)


def test_path_candidates(test_name: str, check_name: str) -> list[str]:
    """The repository paths a run's test name may stand for, most likely
    first: an integration job's names are module paths under the integration
    tree, a stateless job's are file names under the query trees."""
    name = str(test_name or "").strip().lstrip("/")
    if not name:
        return []
    if name.startswith("tests/"):
        return [name]
    integration = "integration" in str(check_name or "").lower() or "/" in name
    if integration:
        return [_INTEGRATION_DIR + name.split("::", 1)[0]]
    out = [d + name for d in _STATELESS_DIRS]
    if name.endswith(".gen.sql"):
        # a run names a rendered template by its output; the repository holds the template
        out += [d + name.removesuffix(".gen.sql") + ".sql.j2" for d in _STATELESS_DIRS]
    return out


def resolve_test_path(test_name: str, check_name: str, live: set[str] | frozenset[str]) -> str:
    """The first candidate path the index holds live, '' for none."""
    for p in test_path_candidates(test_name, check_name):
        if p in live:
            return p
    return ""


def pair_evidence(commit: str, ranges: list[Range], specific_lines: int) -> str:
    """The compact JSON a `coverage` row carries: the run's commit, the
    specific ranges (the widest MAX_EVIDENCE_RANGES, then re-sorted) and their
    line count."""
    kept = ranges
    if len(kept) > MAX_EVIDENCE_RANGES:
        kept = sorted(sorted(kept, key=lambda r: r[0] - r[1])[:MAX_EVIDENCE_RANGES])
    return json.dumps(
        {"commit": commit, "lines": [list(r) for r in kept], "specific_lines": int(specific_lines)},
        separators=(",", ":"),
    )


def evidence_ranges(evidence: str) -> list[Range]:
    """The ranges of a `coverage` row's evidence, [] when it holds none."""
    try:
        data = json.loads(evidence or "{}")
    except ValueError:
        return []
    raw = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return merge_ranges(r for r in raw if isinstance(r, list) and len(r) >= 2)


def json_ranges(raw: str) -> list[Range]:
    """A `code_coverage_files.lines` value as merged ranges."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return merge_ranges(r for r in data if isinstance(r, list) and len(r) >= 2)


@dataclass
class ImportPlan:
    """What one import writes and retires, the counts a dry run prints."""

    pairs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    retire_pairs: set[tuple[str, str]] = field(default_factory=set)
    retire_files: set[str] = field(default_factory=set)
    unmapped_tests: set[str] = field(default_factory=set)
    unindexed_files: int = 0


def retirements(
    plan: ImportPlan, live_pairs: set[tuple[str, str]], live_files: set[str]
) -> ImportPlan:
    """Fill the plan's retirements: the live rows its run no longer has."""
    plan.retire_pairs = live_pairs - set(plan.pairs)
    plan.retire_files = live_files - set(plan.files)
    return plan


__all__ = [
    "MAX_EVIDENCE_RANGES",
    "ImportPlan",
    "Range",
    "evidence_ranges",
    "intersects",
    "json_ranges",
    "lines_in",
    "merge_ranges",
    "pair_evidence",
    "resolve_test_path",
    "retirements",
    "specificity",
    "test_path_candidates",
]
