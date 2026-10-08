"""The fix's own lines name the change that broke them.

When a fix for the regression exists, the lines it rewrote are the lines that were wrong.
Blaming them at the fix's parent names the merged PR that last wrote them. Blame runs through
`code.blame_range` (blank / comment / format-only lines dropped, `-w -M`). On the benchmark's
regressions the culprit's files often share no path with the report (a crash in
`optimizeJoin.cpp` caused by a change to `KeyCondition.cpp`), so no report-side scan could
reach it; the fix's diff does.

Pure parsing lives here; the blame itself is `code.blame_range` at the
parent, so commit → PR mapping stays in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# `@@ -a,b +c,d @@` — b defaults to 1 when absent
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_OLD_FILE_RE = re.compile(r"^--- a/(.+)$")
# where a fix's lines say nothing about the culprit: tests, docs, CI, data
_SKIP_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|docs?|ci|\.github|contrib|benchmark|utils/check-style)/|"
    r"\.(?:md|reference|sql|sh|txt|xml|yaml|yml|json|py)$"
)
# bounds: a fix rewriting 40 hunks is a refactor; blame the biggest few
MAX_RANGES = 8
MAX_LINES_PER_RANGE = 40
# a pure insertion blames the line above it, and counts for less
INSERTION_WEIGHT = 0.5


@dataclass(frozen=True)
class Range:
    path: str
    start: int
    end: int
    weight: float = 1.0

    @property
    def lines(self) -> int:
        return self.end - self.start + 1


@dataclass
class Vote:
    """One blamed PR's evidence across the fix's ranges."""

    pr_number: int
    lines: float = 0.0
    ranges: int = 0
    paths: list[str] = field(default_factory=list)
    title: str = ""
    author: str = ""
    merged_at: str = ""
    first_release: str = ""
    url: str = ""


def pre_image_ranges(
    diff: str, *, max_ranges: int = MAX_RANGES, max_lines: int = MAX_LINES_PER_RANGE
) -> list[Range]:
    """The pre-image line ranges a `git diff -U0 parent..merge` rewrote or
    deleted, per source file (tests, docs, CI and data files skipped); a
    pure insertion (`-a,0`) stands for the line it follows, at half weight.
    The biggest ranges first, capped; a range longer than `max_lines` is cut
    to its head (the first rewritten lines carry the changed logic)."""
    out: list[Range] = []
    path = ""
    old_path = ""
    for line in (diff or "").splitlines():
        m_old = _OLD_FILE_RE.match(line)
        if m_old:
            old_path = m_old.group(1)
            continue
        m_file = _FILE_RE.match(line)
        if m_file:
            path = old_path or m_file.group(1)
            old_path = ""
            continue
        if line.startswith("+++ /dev/null"):
            path = old_path
            old_path = ""
            continue
        m = _HUNK_RE.match(line)
        if not m or not path or _SKIP_PATH_RE.search(path):
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count > 0:
            end = start + min(count, max_lines) - 1
            out.append(Range(path=path, start=start, end=end))
        elif start > 0:
            out.append(Range(path=path, start=start, end=start, weight=INSERTION_WEIGHT))
    out.sort(key=lambda r: (-(r.lines * r.weight), r.path, r.start))
    return out[:max_ranges]


def tally(
    per_range: list[tuple[Range, list[dict[str, object]]]], *, exclude: set[int]
) -> list[Vote]:
    """Votes per blamed PR: each range's blamed candidates share its weighted
    lines by their `lines_blamed` (the first candidate takes the range when
    blame reports no counts); the fix itself and `exclude` never vote.
    Strongest first: lines, then ranges, then the older merge (the earlier
    change is the one the later ones built on)."""
    votes: dict[int, Vote] = {}
    for rng, cands in per_range:
        rows = [c for c in cands if int(c.get("pr_number") or 0) not in exclude]  # type: ignore[call-overload]
        if not rows:
            continue
        counts = [int(c.get("lines_blamed") or 0) for c in rows]  # type: ignore[call-overload]
        total = sum(counts)
        for c, n in zip(rows, counts, strict=True):
            share = (n / total) if total else (1.0 if c is rows[0] else 0.0)
            if share <= 0:
                continue
            pr = int(c.get("pr_number") or 0)  # type: ignore[call-overload]
            v = votes.setdefault(pr, Vote(pr_number=pr))
            v.lines += share * rng.lines * rng.weight
            v.ranges += 1
            if rng.path not in v.paths:
                v.paths.append(rng.path)
            for k in ("title", "author", "merged_at", "first_release", "url"):
                if not getattr(v, k) and c.get(k):
                    setattr(v, k, str(c.get(k)))
    return sorted(votes.values(), key=lambda v: (-v.lines, -v.ranges, v.merged_at))


def _minor(text: str) -> tuple[int, int] | None:
    m = re.match(r"^\s*v?(\d+)\.(\d+)", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def rank_votes(
    votes: list[Vote], *, pool_scores: dict[int, float], good: str = "", bad: str = ""
) -> list[Vote]:
    """The blamed PRs in the order they lead the bisect: blame's weighted lines, plus
    the report-side scan's own score when the PR is also in the pool (it touches the
    report's files, frames or patterns), each as a share of its maximum; a PR first
    released outside the good..bad window goes last — the bug appeared inside it."""
    if not votes:
        return votes
    top_lines = max(v.lines for v in votes) or 1.0
    top_score = max(pool_scores.values(), default=0.0) or 1.0
    lo, hi = _minor(good), _minor(bad)

    def outside(v: Vote) -> bool:
        rel = _minor(v.first_release)
        if rel is None:
            return False
        return bool((lo and rel <= lo) or (hi and rel > hi))

    def key(v: Vote) -> tuple[bool, float, str]:
        s = v.lines / top_lines + float(pool_scores.get(v.pr_number, 0.0)) / top_score
        return (outside(v), -s, v.merged_at)

    ranked = sorted(votes, key=key)
    for v in ranked:
        if outside(v):
            v.lines *= OUTSIDE_WINDOW_WEIGHT
    return ranked


# a blamed PR first released outside good..bad keeps this share of its lines
OUTSIDE_WINDOW_WEIGHT = 0.25
