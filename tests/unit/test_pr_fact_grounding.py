"""Unit tests for PR-fact grounding number extraction (_referenced_pr_numbers).
The DB-backed injection is covered by an e2e smoke; here we pin the pure
extraction that decides which PRs to ground."""

from __future__ import annotations

from server.memory.app.code_grounding import _referenced_pr_numbers


class _M:
    def __init__(self, content):
        self.content_excerpt = content


def test_extracts_from_query_and_memories():
    nums = _referenced_pr_numbers(
        "status of PR 117428?",
        [_M("revert #117651 landed"), _M("issue 68601 is about row policy")],
    )
    assert set(nums) == {117428, 117651, 68601}


def test_ignores_years_and_short_numbers():
    # 4-6 digit gate: '2026' (year, 4-digit) matches only if PR/#-prefixed;
    # a bare '42' never matches.
    nums = _referenced_pr_numbers("in 2026 we fixed 42 bugs", [])
    assert nums == []


def test_hash_and_pr_forms():
    nums = _referenced_pr_numbers("#117207 and PR 104884 and pull request 110535", [])
    assert set(nums) == {117207, 104884, 110535}


def test_dedup_and_cap():
    mems = [_M(f"#{100000 + i}") for i in range(20)]
    nums = _referenced_pr_numbers("", mems)
    assert len(nums) <= 12
    assert len(set(nums)) == len(nums)


def test_empty_input():
    assert _referenced_pr_numbers("", []) == []
