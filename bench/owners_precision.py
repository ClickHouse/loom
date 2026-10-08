#!/usr/bin/env python3
"""Owners precision bench — does code.impact's `owners` name the people who
actually review changes there?

`owners` (server/code/domain/index/owners.rank_owners) is recency-
weighted PR authorship over the paths a change touches. The claim behind it
is that the people who recently merged code in an area are the people to
ask about a change to it. This bench scores that claim offline against the
one ground truth loom holds: the humans who left inline review comments on
each merged PR (loom.code_pr_reviews).

Protocol (leakage-free): for each held-out merged PR P with >= 1 human
reviewer, the candidate pool is every PR merged strictly BEFORE P whose
files intersect P's files, P's own author excluded; rank_owners is run
with now = P.merged_at. A hit is an owner who reviewed P.

  coverage       share of held-out PRs with a non-empty owners list
  hit@1 / hit@3  share of PRs whose top-1 / any owner reviewed it
  precision      owners who reviewed / owners named (over PRs with owners)
  baseline hit@3 the three most active reviewers over every loaded PR,
                 named for every PR (it counts the held-out PRs' own
                 reviewers, so it is an upper-biased comparator)
  baseline_prior hit@3  the same list computed per held-out PR over PRs
                 merged before it — the leakage-free baseline

Two signals feed rank_owners: who AUTHORED the pool PRs and who REVIEWED
them (loom.code_pr_reviews, humans only; every pool PR merged before P so
its reviews are prior knowledge). --review-weight scales the second;
--sweep runs 0 (authorship only), 0.5, 1, 2, reviewers-only and the
shipped configuration (reviews at 1 + namespace-prior fill) in one load.

A sample reading (300 held-out, 948 PRs with reviews backfilled):
  authors_only 0.337  review_w1 0.360  review_w1+global 0.447  baseline 0.370

House conventions: pure scoring core above the live-path banner (unit-tested
in bench/test_owners_precision.py, CH-free); --dry self-asserts on
synthetic data. The live path reads ClickHouse through server.core.ch, so run it
from the repo root with .env present (LOOM_CH_*):

  python bench/owners_precision.py --dry
  python bench/owners_precision.py --org <org> --namespace code-clickhouse \\
      --days 90 --limit 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, ".")

from server.code.domain.index.owners import is_bot, rank_owners


@dataclass
class PR:
    number: int
    author: str
    files: list[str]
    merged_at: datetime
    assignees: list[str] = field(default_factory=list)


@dataclass
class Summary:
    holdout: int = 0
    covered: int = 0
    hit1: int = 0
    hit3: int = 0
    named: int = 0
    named_correct: int = 0
    baseline_hit3: int = 0
    baseline_prior_hit3: int = 0
    baseline: list[str] = field(default_factory=list)
    filled_prs: int = 0  # held-out PRs where the namespace prior added a name

    def as_dict(self) -> dict[str, Any]:
        n = max(1, self.holdout)
        return {
            "holdout_prs": self.holdout,
            "coverage": round(self.covered / n, 3),
            "hit@1": round(self.hit1 / n, 3),
            "hit@3": round(self.hit3 / n, 3),
            "precision": round(self.named_correct / max(1, self.named), 3),
            "baseline_hit@3": round(self.baseline_hit3 / n, 3),
            "baseline_prior_hit@3": round(self.baseline_prior_hit3 / n, 3),
            "baseline": self.baseline,
            "names_per_pr": round(self.named / n, 2),
            "filled_share": round(self.filled_prs / n, 3),
        }


# ---------------------------------------------------------------------------
# Pure scoring core
# ---------------------------------------------------------------------------


def reviewers_of(pr: PR, comments: list[dict[str, Any]]) -> set[str]:
    """Human reviewers of a PR: distinct comment authors, bots and the PR's
    own author excluded."""
    out: set[str] = set()
    for c in comments:
        a = str(c.get("author") or "").strip()
        if not a or c.get("is_bot") or is_bot(a) or a.lower() == pr.author.lower():
            continue
        out.add(a)
    return out


def assigned_of(pr: PR) -> set[str]:
    """People the repository put on the PR to review it (assignees ∪
    requested reviewers, code_pr_history.assignees), bots and the author
    excluded."""
    return {a for a in pr.assignees if a and not is_bot(a) and a.lower() != pr.author.lower()}


def truth_of(pr: PR, comments: list[dict[str, Any]], mode: str = "inline") -> set[str]:
    """Ground truth for one held-out PR: "inline" = who left an inline
    comment (the original, misses silent approvers); "assigned" = who the
    repo assigned; "both" = union."""
    if mode == "inline":
        return reviewers_of(pr, comments)
    if mode == "assigned":
        return assigned_of(pr)
    return reviewers_of(pr, comments) | assigned_of(pr)


def reviewer_comment_counts(pr: PR, comments: list[dict[str, Any]]) -> dict[str, int]:
    """Human inline comments per reviewer on one PR (same exclusions as
    reviewers_of) — a volume signal for weighting review expertise."""
    out: dict[str, int] = {}
    for c in comments:
        a = str(c.get("author") or "").strip()
        if not a or c.get("is_bot") or is_bot(a) or a.lower() == pr.author.lower():
            continue
        out[a] = out.get(a, 0) + 1
    return out


def candidate_pool(
    target: PR,
    prs: list[PR],
    reviews: dict[int, list[dict[str, Any]]] | None = None,
    *,
    comment_weighted: bool = False,
    assignee_signal: bool = False,
) -> list[dict[str, Any]]:
    """PRs merged strictly before `target` touching any of its files, as
    rank_owners rows (with each PR's human reviewers when `reviews` is
    given; with comment_weighted, each reviewer carries log1p(comments) as
    its multiplier). Excludes the target itself."""
    import math

    want = set(target.files)
    reviews = reviews or {}
    rows: list[dict[str, Any]] = []
    for p in prs:
        if p.number == target.number or p.merged_at >= target.merged_at or want.isdisjoint(p.files):
            continue
        comments = reviews.get(p.number, [])
        people = reviewers_of(p, comments)
        if assignee_signal:
            people = people | assigned_of(p)
        row: dict[str, Any] = {
            "author": p.author,
            "merged_at": p.merged_at,
            "files": p.files,
            "reviewers": sorted(people),
        }
        if comment_weighted:
            row["reviewer_weights"] = {
                a: math.log1p(n) for a, n in reviewer_comment_counts(p, comments).items()
            }
        rows.append(row)
    return rows


def score(
    prs: list[PR],
    reviews: dict[int, list[dict[str, Any]]],
    *,
    limit: int,
    review_weight: float = 0.0,
    author_weight: float = 1.0,
    fill_global: bool = False,
    comment_weighted: bool = False,
    truth: str = "inline",
    assignee_signal: bool = False,
    fill_policy: str = "",
    weak_pool: int = 10,
    weak_share: float = 0.25,
    fill_max: int = 3,
    since: datetime | None = None,
) -> Summary:
    """fill_global: when the area ranking names fewer than three people,
    top up with the namespace's busiest human reviewers computed over PRs
    merged BEFORE the held-out one (the baseline's prior, leakage-free) —
    measures whether a global prior should back the area signal.

    fill_policy refines WHEN the prior may add names (recommending nobody
    can beat recommending a random person):
      "always"  whenever fewer than three area names (== fill_global)
      "empty"   only when the area named nobody
      "weak"    only when the area evidence is thin: fewer than `weak_pool`
                pool PRs, or the top area share below `weak_share`
      "never"   area names only
    fill_max caps how many prior names may be added (1 = one fallback).
    since bounds the held-out set to PRs merged at or after it."""
    s = Summary()
    holdout = [
        p
        for p in sorted(prs, key=lambda p: p.merged_at, reverse=True)
        if (since is None or p.merged_at >= since) and truth_of(p, reviews.get(p.number, []), truth)
    ][:limit]
    # baseline: the namespace's three most active human reviewers overall
    counts: Counter[str] = Counter()
    for p in prs:
        counts.update(truth_of(p, reviews.get(p.number, []), truth))
    s.baseline = [a for a, _ in counts.most_common(3)]
    for p in holdout:
        s.holdout += 1
        truth_set = truth_of(p, reviews.get(p.number, []), truth)
        if set(s.baseline) & truth_set:
            s.baseline_hit3 += 1
        prior: Counter[str] = Counter()
        for q in prs:
            if q.merged_at < p.merged_at:
                prior.update(truth_of(q, reviews.get(q.number, []), truth))
        if {a for a, _ in prior.most_common(3)} & truth_set:
            s.baseline_prior_hit3 += 1
        owners = rank_owners(
            candidate_pool(
                p, prs, reviews, comment_weighted=comment_weighted, assignee_signal=assignee_signal
            ),
            now=p.merged_at,
            exclude=(p.author,),
            review_weight=review_weight,
            author_weight=author_weight,
        )
        names = [o["author"] for o in owners]
        policy = fill_policy or ("always" if fill_global else "never")
        pool_n = len(candidate_pool(p, prs, reviews, assignee_signal=assignee_signal))
        top_share = float(owners[0]["share"]) if owners else 0.0
        may_fill = len(names) < 3 and (
            policy == "always"
            or (policy == "empty" and not names)
            or (policy == "weak" and (pool_n < weak_pool or top_share < weak_share))
        )
        if may_fill:
            added = 0
            for a, _ in prior.most_common(6):
                if len(names) >= 3 or added >= fill_max:
                    break
                if a.lower() != p.author.lower() and a not in names:
                    names.append(a)
                    added += 1
            if added:
                s.filled_prs += 1
        if not names:
            continue
        s.covered += 1
        s.named += len(names)
        s.named_correct += sum(1 for n in names if n in truth_set)
        if names[0] in truth_set:
            s.hit1 += 1
        if set(names) & truth_set:
            s.hit3 += 1
    return s


# ---------------------------------------------------------------------------
# Live path (ClickHouse through server.core.ch; needs .env in cwd)
# ---------------------------------------------------------------------------


async def load(org: str, ns: str, days: int) -> tuple[list[PR], dict[int, list[dict[str, Any]]]]:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    since = datetime.now(UTC) - timedelta(days=days + 400)  # pool reaches back 400 days
    rows = await c.query_all(
        """
        SELECT pr_number, author, files, merged_at, assignees
        FROM loom.code_pr_history FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
          AND merged_at >= {since:DateTime}
        """,
        {"org": org, "ns": ns, "since": since.replace(tzinfo=None)},
    )
    prs = [
        PR(
            int(r["pr_number"]),
            str(r["author"] or ""),
            [str(f) for f in (r["files"] or [])],
            _dt(r["merged_at"]),
            [str(a) for a in (r.get("assignees") or [])],
        )
        for r in rows
    ]
    # reviews for every loaded PR: the pool's reviews are the reviewer
    # signal, the held-out window's reviews are the truth
    numbers = [p.number for p in prs]
    reviews: dict[int, list[dict[str, Any]]] = {}
    for i in range(0, len(numbers), 500):
        batch = numbers[i : i + 500]
        rr = await c.query_all(
            """
            SELECT pr_number, author, is_bot
            FROM loom.code_pr_reviews FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
              AND pr_number IN {nums:Array(UInt32)}
            """,
            {"org": org, "ns": ns, "nums": batch},
        )
        for r in rr:
            reviews.setdefault(int(r["pr_number"]), []).append(
                {"author": str(r["author"] or ""), "is_bot": bool(r.get("is_bot"))}
            )
    return prs, reviews


def _dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(v)).replace(tzinfo=UTC)


def _dry() -> int:
    base = datetime(2026, 9, 1, tzinfo=UTC)
    prs = [
        PR(i, "alice" if i % 2 else "bob", ["src/x.cpp"], base - timedelta(days=30 - i))
        for i in range(1, 21)
    ]
    prs.append(PR(99, "carol", ["src/x.cpp"], base))  # held-out: alice and bob own the file
    reviews = {99: [{"author": "alice", "is_bot": False}, {"author": "robot-ci", "is_bot": True}]}
    s = score(prs, reviews, limit=10)
    d = s.as_dict()
    assert d["holdout_prs"] == 1 and d["coverage"] == 1.0 and d["hit@3"] == 1.0, d
    print(json.dumps(d, indent=2))
    print("--dry self-check passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--org", default="dev")
    ap.add_argument("--namespace", default="code-clickhouse")
    ap.add_argument("--days", type=int, default=90, help="held-out window (merged within N days)")
    ap.add_argument("--limit", type=int, default=300, help="max held-out PRs, newest first")
    ap.add_argument(
        "--review-weight",
        type=float,
        default=None,
        help="weight of one review vs one authored PR (default: the server's REVIEW_WEIGHT)",
    )
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="score authorship-only, review weights 0.5/1/2 and reviewers-only in one load",
    )
    ap.add_argument(
        "--truth",
        choices=("inline", "assigned", "both"),
        default="inline",
        help="ground truth: inline commenters (original), repo assignees, or both",
    )
    ap.add_argument(
        "--fill-policy",
        choices=("always", "empty", "weak", "never"),
        default="always",
        help="when the namespace prior may add names (see score()); the server fills always",
    )
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.dry:
        return _dry()
    from server.code.domain.index.owners import REVIEW_WEIGHT

    prs, reviews = asyncio.run(load(args.org, args.namespace, args.days))
    since = datetime.now(UTC) - timedelta(days=args.days)
    with_assigned = sum(1 for p in prs if assigned_of(p))
    # (name, review_weight, author_weight, fill_global, comment_weighted, assignee_signal)
    arms: list[tuple[str, float, float, bool, bool, bool]]
    if args.sweep:
        arms = [
            ("authors_only", 0.0, 1.0, False, False, False),
            ("review_w0.5", 0.5, 1.0, False, False, False),
            ("review_w1", 1.0, 1.0, False, False, False),
            ("review_w2", 2.0, 1.0, False, False, False),
            ("reviewers_only", 1.0, 0.0, False, False, False),
            # a reviewer's weight on a PR scales with log1p(comments)
            ("review_w1_cmt", 1.0, 1.0, False, True, False),
            ("review_w1+global", 1.0, 1.0, True, False, False),
            ("review_w1_cmt+global", 1.0, 1.0, True, True, False),
            # assignees of pool PRs earn review weight
            ("review_w1+asg", 1.0, 1.0, False, False, True),
            ("review_w1+asg+global", 1.0, 1.0, True, False, True),
        ]
    else:
        # the server's configuration: reviews + assignees weighted, namespace prior fill
        rw = REVIEW_WEIGHT if args.review_weight is None else args.review_weight
        arms = [(f"review_w{rw:g}+asg+fill_{args.fill_policy}", rw, 1.0, True, False, True)]
    out: dict[str, Any] = {
        "prs_loaded": len(prs),
        "prs_with_reviews": len(reviews),
        "prs_with_assignees": with_assigned,
        "truth": args.truth,
    }
    for name, rw, aw, fg, cw, asg in arms:
        out[name] = score(
            prs,
            reviews,
            limit=args.limit,
            review_weight=rw,
            author_weight=aw,
            fill_global=fg,
            comment_weighted=cw,
            truth=args.truth,
            assignee_signal=asg,
            fill_policy="" if args.sweep else args.fill_policy,
            since=since,
        ).as_dict()
    if args.json:
        print(json.dumps(out))
    else:
        print("=== owners_precision ===")
        print(
            f"  prs_loaded {out['prs_loaded']}  prs_with_reviews {out['prs_with_reviews']}  "
            f"prs_with_assignees {out['prs_with_assignees']}  truth={out['truth']}"
        )
        for name, _, _, _, _, _ in arms:
            d = out[name]
            print(
                f"  {name:20} coverage {d['coverage']:.3f}  hit@1 {d['hit@1']:.3f}  "
                f"hit@3 {d['hit@3']:.3f}  precision {d['precision']:.3f}  "
                f"baseline_hit@3 {d['baseline_hit@3']:.3f}  "
                f"baseline_prior_hit@3 {d['baseline_prior_hit@3']:.3f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
