"""The commit-coverage memos are bounded by clearing when full. The call
that fills a memo still answers from what it just read: the answer is
immutable history, and the clear only makes room."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import commit_coverage as cc
from server.code.app.index import gitrepo


@pytest.fixture(autouse=True)
def _fresh_memos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "_MEMO", {})
    monkeypatch.setattr(cc, "_COUNT_MEMO", {})
    monkeypatch.setattr(cc, "_PATHS_MEMO", {})


def test_the_ancestry_memo_answers_the_call_that_fills_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def is_ancestor(*_a: Any) -> bool:
        return True

    monkeypatch.setattr(gitrepo, "is_ancestor", is_ancestor)
    monkeypatch.setattr(cc, "_MEMO_MAX", 2)
    answers = [asyncio.run(cc._covered("/g", f"{i:040x}", "f" * 40)) for i in range(5)]
    assert answers == [True] * 5
    assert len(cc._MEMO) <= 2


def test_the_count_memo_answers_the_call_that_fills_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def commit_count_between(*_a: Any) -> int:
        return 5

    monkeypatch.setattr(gitrepo, "commit_count_between", commit_count_between)
    monkeypatch.setattr(cc, "_MEMO_MAX", 2)
    counts = [asyncio.run(cc._commits_since("/g", f"{i:040x}", "f" * 40)) for i in range(3)]
    assert counts == [5, 5, 5]
    assert len(cc._COUNT_MEMO) <= 2


def test_a_failed_read_is_not_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def diff_name_only(*_a: Any, **_k: Any) -> list[str]:
        calls.append("diff")
        raise gitrepo.GitError("unknown revision")

    monkeypatch.setattr(gitrepo, "diff_name_only", diff_name_only)
    assert asyncio.run(cc._paths_since("/g", "a" * 40, "f" * 40)) is None
    assert asyncio.run(cc._paths_since("/g", "a" * 40, "f" * 40)) is None
    assert calls == ["diff", "diff"] and cc._PATHS_MEMO == {}


def test_the_paths_memo_answers_the_call_that_fills_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def diff_name_only(*_a: Any, **_k: Any) -> list[str]:
        return ["src/a.cpp"]

    monkeypatch.setattr(gitrepo, "diff_name_only", diff_name_only)
    monkeypatch.setattr(cc, "_PATHS_MEMO_MAX", 2)
    answers = [asyncio.run(cc._paths_since("/g", f"{i:040x}", "f" * 40)) for i in range(3)]
    assert answers == [["src/a.cpp"]] * 3
    assert len(cc._PATHS_MEMO) <= 2
