"""code.impact owners: recency-weighted PR authorship, bots and the change's
own author excluded, a share floor instead of a guessed name."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from server.code.domain.index.owners import is_bot, rank_owners
from server.settings import settings

NOW = datetime(2026, 9, 11, tzinfo=UTC)


def _pr(author: str, days_ago: float) -> dict:
    return {"author": author, "merged_at": NOW - timedelta(days=days_ago), "files": ["src/x.cpp"]}


def test_recent_authorship_outranks_volume_long_ago() -> None:
    prs = [_pr("alice", 10), _pr("alice", 20), _pr("alice", 30)] + [_pr("bob", 300)] * 6
    out = rank_owners(prs, now=NOW)
    assert [o["author"] for o in out] == ["alice", "bob"]
    assert out[0]["prs"] == 3 and out[0]["share"] > 0.7, out
    # alice: three PRs at ~0.9 each ≈ 2.68; bob: six at 0.5^(300/120) ≈ 0.18 each ≈ 1.06.
    # Twice the PRs, a third of the weight. At 500 days bob drops below the floor entirely:
    assert [o["author"] for o in rank_owners(prs[:3] + [_pr("bob", 500)] * 6, now=NOW)] == ["alice"]


def test_share_floor_and_cap() -> None:
    prs = (
        [_pr("a", 1)] * 10
        + [_pr("b", 1)] * 10
        + [_pr("c", 1)] * 10
        + [_pr("d", 1)] * 10
        + [_pr("e", 1)]
    )
    out = rank_owners(prs, now=NOW)
    assert [o["author"] for o in out] == ["a", "b", "c"], "at most three"
    assert all(o["share"] >= 0.15 for o in out)
    out2 = rank_owners([_pr("a", 1)] * 20 + [_pr("z", 1)], now=NOW)
    assert [o["author"] for o in out2] == ["a"], "z at 1/21 share is below every floor, not listed"


def test_hub_file_relative_floor_names_the_next_two_behind_a_clear_leader() -> None:
    # a typical hot file's shape: leader ~24%, then ~12%, ~9%, then a long tail
    prs = (
        [_pr("oliver", 1)] * 24
        + [_pr("mika", 1)] * 12
        + [_pr("rafa", 1)] * 9
        + [_pr(f"t{i}", 1) for i in range(55)]
    )
    out = rank_owners(prs, now=NOW)
    assert [o["author"] for o in out] == ["oliver", "mika", "rafa"]
    # floor = max(0.08, min(0.15, 0.24 / 3 = 0.08)) = 0.08: rafa at 9% is in, the 1% tail is out
    assert all(o["share"] >= 0.08 for o in out) and out[-1]["author"] == "rafa"


def test_agent_accounts_are_not_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_bot_authors", "supportbot")
    out = rank_owners(
        [_pr("supportbot", 1)] * 30 + [_pr("robot-clickhouse-ci-1", 1)] * 5 + [_pr("alice", 2)],
        now=NOW,
    )
    assert [o["author"] for o in out] == ["alice"]


def test_bots_and_excluded_authors_redistribute_the_mass() -> None:
    prs = (
        [_pr("robot-clickhouse", 1)] * 20
        + [_pr("dependabot[bot]", 1)] * 5
        + [_pr("alice", 5), _pr("me", 2)]
    )
    out = rank_owners(prs, now=NOW, exclude=("Me",))
    assert [o["author"] for o in out] == ["alice"]
    assert out[0]["share"] == 1.0, (
        "excluded weight is removed from the denominator, not left as a hole"
    )
    assert is_bot("github-actions") and is_bot("x-bot") and is_bot("") and not is_bot("alice")


def test_missing_merged_at_counts_as_old_and_empty_input_yields_nobody() -> None:
    out = rank_owners([{"author": "alice", "merged_at": None}, _pr("bob", 1)], now=NOW)
    assert [o["author"] for o in out] == ["bob"], (
        "unknown date decays to ~4 half-lives, below the floor"
    )
    assert rank_owners([], now=NOW) == []
    assert rank_owners([_pr("robot-ci", 1)], now=NOW) == []


def test_iso_string_dates_are_accepted() -> None:
    out = rank_owners([{"author": "alice", "merged_at": "2026-09-10T12:00:00Z"}], now=NOW)
    assert out[0]["author"] == "alice" and out[0]["last_merged_at"].startswith("2026-09-10")


def test_reviewers_of_pool_prs_earn_weight() -> None:
    """The bench's ground truth is who reviewed; a person who reviewed the
    last three PRs in an area ranks with (or above) their authors. A
    reviewer who is also the author counts once, as author."""
    prs = [
        dict(_pr("alice", 5), reviewers=["Reviewer", "alice", "robot-ch-test-poll"]),
        dict(_pr("bob", 10), reviewers=["reviewer"]),
        dict(_pr("carol", 20), reviewers=["reviewer", "dave"]),
    ]
    out = rank_owners(prs, now=NOW, review_weight=1.0)
    assert out[0]["author"] in ("Reviewer", "reviewer"), out
    top = out[0]
    assert top["reviews"] == 3 and top["prs"] == 0
    # case-insensitive dedupe within a PR, but the first spelling is kept per key
    assert all(o["author"] != "robot-ch-test-poll" for o in out)
    alice = next(o for o in out if o["author"] == "alice")
    assert alice["prs"] == 1 and alice["reviews"] == 0, "author of a PR is not also its reviewer"
    # review_weight=0 reproduces the authorship-only ranking exactly
    assert [o["author"] for o in rank_owners(prs, now=NOW, review_weight=0.0)] == [
        o["author"]
        for o in rank_owners([_pr("alice", 5), _pr("bob", 10), _pr("carol", 20)], now=NOW)
    ]
    # exclude applies to reviewers too
    assert all(
        o["author"].lower() != "reviewer" for o in rank_owners(prs, now=NOW, exclude=("reviewer",))
    )
    # authors-only off: reviewers alone
    only = rank_owners(prs, now=NOW, author_weight=0.0)
    assert {o["author"].lower() for o in only} <= {"reviewer", "dave"}


def test_reviewer_weights_scale_a_reviewers_credit_per_pr() -> None:
    """Review-comment volume as a per-reviewer multiplier: noise on the bench
    (+0.003), kept as a hook. Missing → 1.0; zero or
    negative → the reviewer earns nothing from that PR."""
    prs = [
        dict(
            _pr("alice", 5), reviewers=["rev", "quiet"], reviewer_weights={"rev": 3.0, "quiet": 0.0}
        ),
        dict(_pr("bob", 6), reviewers=["other"]),  # no weights: multiplier 1
    ]
    out = {
        o["author"]: o
        for o in rank_owners(prs, now=NOW, review_weight=1.0, min_share=0.0, max_owners=5)
    }
    assert "quiet" not in out, "zero multiplier earns nothing"
    assert out["rev"]["share"] > out["other"]["share"] > 0
    assert out["rev"]["reviews"] == 1 and out["other"]["reviews"] == 1
    # weights keyed by casefold also resolve
    prs2 = [dict(_pr("alice", 5), reviewers=["Rev"], reviewer_weights={"rev": 2.0})]
    assert rank_owners(prs2, now=NOW, review_weight=1.0)[0]["author"] == "Rev"
