"""Pure helpers for `code.symbol_history`: one function's regression history, assembled
from loom's merged-PR record (files, linked issues, reverts, inline review comments) and
— when the tracker namespace is bound and reachable — the tracker's per-PR facts
(whether the diff names the symbol, test presence, CI result, first release, what the PR
caused) and the open issues that name the symbol today.

The loom record is file-level: a PR that touched the file is a PR that
MAY have touched the function. The QA diff text settles it per PR
(`touches_symbol` true / false); without QA it stays `unknown`. Every
count is a floor, and the summary says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

HISTORY = "history"
NO_HISTORY = "no_history"
UNKNOWN = "unknown"

_FIX_RE = re.compile(
    r"(?i)\b(fix|fixes|fixed|fixing|bug|crash|regression|logical error|segfault|"
    r"deadlock|race|leak|hang|assert|ub|use-after-free|overflow)\b"
)
_BACKPORT_RE = re.compile(r"(?i)^\s*backport\b")


@dataclass
class PrRow:
    pr_number: int
    title: str = ""
    author: str = ""
    merged_at: str = ""
    is_revert: bool = False
    is_fix: bool = False
    is_backport: bool = False
    linked_issues: list[dict[str, Any]] = field(default_factory=list)
    comments_on_symbol: int = 0
    # true (QA diff names it) | named (loom: title or body names it) |
    # false (QA diff does not) | unknown
    touches_symbol: str = "unknown"
    tracker: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pr_number": self.pr_number,
            "title": self.title[:160],
            "author": self.author,
            "merged_at": self.merged_at,
            "is_revert": self.is_revert,
            "is_fix": self.is_fix,
            "touches_symbol": self.touches_symbol,
        }
        if self.is_backport:
            out["is_backport"] = True
        if self.linked_issues:
            out["linked_issues"] = self.linked_issues[:6]
        if self.comments_on_symbol:
            out["comments_on_symbol"] = self.comments_on_symbol
        if self.tracker:
            out["qa"] = self.tracker
        return out


def _iso(v: Any) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else str(v or "")


def is_fix_title(title: str, linked: list[int]) -> bool:
    """A PR that closes an issue or says fix/crash/regression in its title."""
    return bool(linked) or bool(_FIX_RE.search(title or ""))


def in_span(line: int, start: int, end: int, *, slack: int = 3) -> bool:
    """A review comment's line falls inside the symbol's span (a few lines
    of slack: the comment is as of the PR's commit, the span as of master)."""
    if line <= 0 or start <= 0:
        return False
    return start - slack <= line <= (end if end >= start else start) + slack


def build_rows(
    prs: list[dict[str, Any]],
    *,
    issue_facts: dict[int, dict[str, Any]],
    comments: list[dict[str, Any]],
    span: tuple[int, int],
    tracker_facts: dict[int, dict[str, Any]],
    tracker_edges: list[dict[str, Any]],
    frames: list[str],
    qa_ok: bool,
) -> list[PrRow]:
    """The timeline rows, newest first, from the loom PR rows and whatever
    QA supplied. A PR whose QA diff names none of `frames` is kept but
    marked `touches_symbol = false` — the file changed, the function did
    not — so the caller can weigh it."""
    by_pr_comments: dict[int, int] = {}
    for c in comments:
        if in_span(int(c.get("line") or 0), span[0], span[1]):
            n = int(c.get("pr_number") or 0)
            by_pr_comments[n] = by_pr_comments.get(n, 0) + 1
    caused_by_pr: dict[int, list[int]] = {}
    fixes_by_pr: dict[int, list[int]] = {}
    for e in tracker_edges:
        src = _num(str(e.get("src_ref") or ""))
        dst = _num(str(e.get("dst_ref") or ""))
        rel, direction = str(e.get("rel_type") or ""), str(e.get("direction") or "")
        if rel == "caused_by" and direction == "in" and src and dst:
            caused_by_pr.setdefault(src, []).append(dst)
        if rel == "fixes" and direction == "out" and src and dst:
            fixes_by_pr.setdefault(src, []).append(dst)
    out: list[PrRow] = []
    for r in prs:
        n = int(r.get("pr_number") or 0)
        title = str(r.get("title") or "")
        linked_nums = [int(x) for x in (r.get("linked_issues") or []) if int(x) > 0]
        for extra in fixes_by_pr.get(n, []):
            if extra not in linked_nums:
                linked_nums.append(extra)
        linked = [
            {
                "number": i,
                **{
                    k: v
                    for k, v in (issue_facts.get(i) or {}).items()
                    if k in ("title", "state", "severity", "component")
                },
            }
            for i in linked_nums[:6]
        ]
        row = PrRow(
            pr_number=n,
            title=title,
            author=str(r.get("author") or ""),
            merged_at=_iso(r.get("merged_at")),
            is_revert=bool(r.get("is_revert")),
            is_fix=is_fix_title(title, linked_nums),
            is_backport=bool(_BACKPORT_RE.match(title)),
            linked_issues=linked,
            comments_on_symbol=by_pr_comments.get(n, 0),
        )
        # loom's own function-level hint: the PR's title or description
        # names the function (a review comment inside the span counts too)
        text = f"{title}\n{r.get('body_excerpt') or ''}"
        if row.comments_on_symbol or any(f and f in text for f in frames):
            row.touches_symbol = "named"
        fact = tracker_facts.get(n)
        if fact is not None:
            matched = [str(x) for x in (fact.get("matched_symbols") or [])]
            row.touches_symbol = "true" if matched else ("false" if frames else "unknown")
            qa: dict[str, Any] = {}
            has_test = fact.get("has_test")
            if has_test is not None and int(has_test) >= 0:
                qa["has_test"] = bool(int(has_test))
            if fact.get("ci_result"):
                qa["ci_result"] = str(fact["ci_result"])
            if fact.get("minor_version"):
                qa["first_release"] = str(fact["minor_version"])
            if caused_by_pr.get(n):
                qa["caused"] = sorted(set(caused_by_pr[n]))[:6]
            if matched:
                qa["names"] = matched[:6]
            row.tracker = qa
        out.append(row)
    out.sort(key=lambda r: r.merged_at, reverse=True)
    return out


def _num(ref: str) -> int:
    _, _, n = ref.partition("#")
    try:
        return int(n)
    except ValueError:
        return 0


def summarize(
    rows: list[PrRow], *, window_prs: int, open_issues: int, qa_ok: bool
) -> dict[str, Any]:
    """Counts a reviewer reads first, and one line saying what they mean.
    Every number is a floor over the PRs considered."""
    touching = [r for r in rows if r.touches_symbol != "false"]
    named = [r for r in touching if r.touches_symbol in ("true", "named")]
    fixes = [r for r in touching if r.is_fix and not r.is_revert]
    named_fixes = [r for r in named if r.is_fix and not r.is_revert]
    reverts = [r for r in touching if r.is_revert]
    caused = sorted({i for r in touching for i in (r.tracker.get("caused") or [])})
    authors: dict[str, int] = {}
    for r in touching:
        if r.author and not r.author.endswith("[bot]") and not r.author.startswith("robot-"):
            authors[r.author] = authors.get(r.author, 0) + 1
    top_authors = [a for a, _ in sorted(authors.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    last = touching[0].merged_at[:10] if touching else ""
    oldest = touching[-1].merged_at[:10] if touching else ""
    parts: list[str] = []
    if touching:
        unit = "this function" if qa_ok else "its file"
        parts.append(
            f"{len(touching)} PR{'s' if len(touching) != 1 else ''} touched {unit} "
            f"in the {window_prs}-PR window ({oldest} → {last})"
        )
    if named:
        parts.append(
            f"{len(named)} name{'' if len(named) != 1 else 's'} the function outright"
            f" ({len(named_fixes)} fix{'es' if len(named_fixes) != 1 else ''})"
        )
    if fixes:
        parts.append(f"{len(fixes)} were fixes")
    if reverts:
        parts.append(f"{len(reverts)} revert{'s' if len(reverts) != 1 else ''}")
    if caused:
        parts.append(
            f"{len(caused)} issue{'s' if len(caused) != 1 else ''} traced back to these PRs"
        )
    if open_issues:
        parts.append(f"{open_issues} open issue{'s' if open_issues != 1 else ''} name it today")
    if not qa_ok:
        parts.append("tracker record unavailable: touch is file-level, not function-level")
    return {
        "prs_total": len(touching),
        "named_prs": len(named),
        "fix_prs": len(fixes),
        "named_fix_prs": len(named_fixes),
        "reverts": len(reverts),
        "caused_issues": caused[:10],
        "open_issues_naming": open_issues,
        "last_touched": last,
        "oldest_in_window": oldest,
        "authors": top_authors,
        "note": (
            ("; ".join(parts) + ".") if parts else "no merged PR in the window touched its file."
        ),
    }


def verdict_of(rows: list[PrRow], *, resolved: bool) -> tuple[str, list[str]]:
    if not resolved:
        return UNKNOWN, ["symbol_unresolved"]
    return (HISTORY if rows else NO_HISTORY), []


__all__ = [
    "HISTORY",
    "NO_HISTORY",
    "UNKNOWN",
    "PrRow",
    "build_rows",
    "in_span",
    "is_fix_title",
    "summarize",
    "verdict_of",
]
