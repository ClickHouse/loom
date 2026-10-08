"""Pure scoring core of bench/owners_precision.py (CH-free)."""

from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import owners_precision as op

BASE = datetime(2026, 9, 1, tzinfo=UTC)


def _pr(n: int, author: str, files: list[str], days_before: int) -> op.PR:
    return op.PR(n, author, files, BASE - timedelta(days=days_before))


def test_reviewers_exclude_bots_and_the_author() -> None:
    p = _pr(1, "carol", ["a"], 0)
    got = op.reviewers_of(
        p,
        [
            {"author": "alice", "is_bot": False},
            {"author": "Carol", "is_bot": False},
            {"author": "robot-ch-test-poll", "is_bot": False},
            {"author": "x[bot]", "is_bot": True},
        ],
    )
    assert got == {"alice"}


def test_candidate_pool_is_strictly_prior_and_file_overlapping() -> None:
    target = _pr(10, "carol", ["src/x.cpp"], 0)
    prs = [
        target,
        _pr(1, "alice", ["src/x.cpp"], 5),
        _pr(2, "bob", ["src/y.cpp"], 5),
        # merged AFTER the target: leakage, excluded
        _pr(3, "dave", ["src/x.cpp"], -1),
    ]
    pool = op.candidate_pool(target, prs)
    assert [r["author"] for r in pool] == ["alice"]


def test_score_hits_precision_and_baseline() -> None:
    prs = [_pr(i, "alice", ["src/x.cpp"], 40 - i) for i in range(1, 11)]
    prs += [_pr(20 + i, "bob", ["src/x.cpp"], 40 - i) for i in range(1, 5)]
    held = _pr(99, "carol", ["src/x.cpp"], 0)
    prs.append(held)
    reviews = {
        99: [{"author": "bob", "is_bot": False}],
        1: [{"author": "erin", "is_bot": False}],
        2: [{"author": "erin", "is_bot": False}],
    }
    s = op.score(prs, reviews, limit=10).as_dict()
    assert s["holdout_prs"] == 3, "held-out = PRs with a human reviewer (99, 1, 2)"
    # PR 99: owners over the prior pool = alice (10 PRs) and bob (4):
    # bob reviewed -> hit@3, not hit@1
    assert s["coverage"] >= 0.33 and s["hit@3"] >= 0.33
    assert 0 < s["precision"] <= 1.0
    assert s["baseline"][0] == "erin", "most active reviewer overall"


def test_candidate_pool_carries_prior_human_reviewers() -> None:
    target = _pr(10, "carol", ["src/x.cpp"], 0)
    prs = [target, _pr(1, "alice", ["src/x.cpp"], 5), _pr(3, "dave", ["src/x.cpp"], -1)]
    reviews = {
        1: [
            {"author": "bob", "is_bot": False},
            {"author": "alice", "is_bot": False},
            {"author": "x[bot]", "is_bot": True},
        ],
        3: [{"author": "leak", "is_bot": False}],
    }
    pool = op.candidate_pool(target, prs, reviews)
    assert pool == [
        {
            "author": "alice",
            "merged_at": prs[1].merged_at,
            "files": ["src/x.cpp"],
            "reviewers": ["bob"],
        }
    ], "own author not a reviewer; later PR is leakage"
    # reviewer signal: with weight, bob (who reviewed the only pool PR) is named
    held = target
    s0 = op.score(
        [held, prs[1]],
        {10: [{"author": "bob", "is_bot": False}], 1: reviews[1]},
        limit=5,
        review_weight=0.0,
    )
    s1 = op.score(
        [held, prs[1]],
        {10: [{"author": "bob", "is_bot": False}], 1: reviews[1]},
        limit=5,
        review_weight=1.0,
    )
    assert s0.hit3 == 0 and s1.hit3 == 1


def test_fill_with_prior_tops_up_to_three_and_labels_the_source() -> None:
    from server.code.domain.index.owners import fill_with_prior

    area = [{"author": "alice", "share": 0.6, "prs": 3, "reviews": 0, "last_merged_at": ""}]
    out = fill_with_prior(area, ["Alice", "bob", "carol", "dave"], exclude=("carol",))
    assert [(o["author"], o["via"]) for o in out] == [
        ("alice", "area"),
        ("bob", "namespace"),
        ("dave", "namespace"),
    ]
    assert out[1]["share"] == 0.0 and out[1]["prs"] == 0
    assert fill_with_prior([], []) == []
    full = [dict(area[0], author=a) for a in ("x", "y", "z")]
    assert [o["via"] for o in fill_with_prior(full, ["q"])] == ["area"] * 3


def test_truth_modes_and_assignee_signal() -> None:
    held = op.PR(10, "carol", ["src/x.cpp"], BASE, ["asg", "carol", "robot-ch-test-poll"])
    prior = op.PR(1, "alice", ["src/x.cpp"], BASE - timedelta(days=3), ["asg"])
    reviews = {10: [{"author": "bob", "is_bot": False}], 1: []}
    assert op.truth_of(held, reviews[10], "inline") == {"bob"}
    assert op.truth_of(held, reviews[10], "assigned") == {"asg"}, "author and bots drop out"
    assert op.truth_of(held, reviews[10], "both") == {"bob", "asg"}
    # the pool row carries assignees only when the signal is on
    assert op.candidate_pool(held, [held, prior], reviews)[0]["reviewers"] == []
    with_asg = op.candidate_pool(held, [held, prior], reviews, assignee_signal=True)
    assert with_asg[0]["reviewers"] == ["asg"]
    # with assigned truth, the assignee signal is what makes the hit
    s0 = op.score([held, prior], reviews, limit=5, review_weight=1.0, truth="assigned")
    s1 = op.score(
        [held, prior], reviews, limit=5, review_weight=1.0, truth="assigned", assignee_signal=True
    )
    assert (s0.hit3, s1.hit3) == (0, 1)


def test_fill_policies_gate_the_prior() -> None:
    held = op.PR(10, "carol", ["src/x.cpp"], BASE, ["asg"])
    # one prior PR by alice → area names alice only; the prior (busiest truth
    # over earlier PRs) would add "asg" and "bob"
    prior = op.PR(1, "alice", ["src/x.cpp"], BASE - timedelta(days=3), ["asg"])
    other = op.PR(2, "dave", ["src/y.cpp"], BASE - timedelta(days=5), ["bob"])
    prs = [held, prior, other]
    reviews: dict[int, list[dict[str, object]]] = {}
    kw = dict(limit=1, review_weight=1.0, truth="assigned", assignee_signal=True)
    never = op.score(prs, reviews, fill_policy="never", **kw)
    assert never.filled_prs == 0 and never.named == 2, "alice + asg from the area"
    empty = op.score(prs, reviews, fill_policy="empty", **kw)
    assert empty.filled_prs == 0, "area named someone, so no fill"
    weak = op.score(prs, reviews, fill_policy="weak", weak_pool=10, **kw)
    assert weak.filled_prs == 1 and weak.named == 3, "one pool PR is thin evidence → filled"
    one = op.score(prs, reviews, fill_policy="always", fill_max=1, **kw)
    assert one.named == 3 and one.as_dict()["names_per_pr"] == 3.0


def test_prior_baseline_never_sees_the_held_out_prs_own_reviewers() -> None:
    old = _pr(1, "alice", ["src/x.cpp"], 10)
    new = _pr(99, "carol", ["src/x.cpp"], 0)
    reviews = {1: [{"author": "erin", "is_bot": False}], 99: [{"author": "bob", "is_bot": False}]}
    s = op.score([old, new], reviews, limit=10).as_dict()
    assert s["holdout_prs"] == 2
    assert s["baseline_hit@3"] == 1.0, "the overall list names each PR's own reviewer"
    assert s["baseline_prior_hit@3"] == 0.0, "no earlier PR was reviewed by bob; none precede PR 1"


def test_since_bounds_the_held_out_set() -> None:
    old = _pr(1, "alice", ["src/x.cpp"], 10)
    new = _pr(99, "carol", ["src/x.cpp"], 0)
    reviews = {1: [{"author": "erin", "is_bot": False}], 99: [{"author": "bob", "is_bot": False}]}
    s = op.score([old, new], reviews, limit=10, since=new.merged_at)
    assert s.holdout == 1
