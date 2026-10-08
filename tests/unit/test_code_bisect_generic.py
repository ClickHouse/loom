"""Bisect holds no deployment's conventions — code paths are
a per-tracker convention, a repository with no tracker (or one whose tracker
holds no changed files) bisects over loom's own PR history, and an agent's
relative time is read."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import server.code.app.bisect as cb
from server.code.app import bisect_scan
from server.code.domain import time_window
from server.tracker.domain.bisect import DEV_BRANCHES, master_reproduces

NOW = datetime(2026, 9, 25, 12, 0, 0)


@pytest.mark.parametrize(
    ("text", "back"),
    [
        ("7 days ago", timedelta(days=7)),
        ("3 weeks ago", timedelta(weeks=3)),
        ("yesterday", timedelta(days=1)),
        ("2 hours ago", timedelta(hours=2)),
        ("last week", timedelta(weeks=1)),
        ("  7  Days  ago ", timedelta(days=7)),
    ],
)
def test_relative_times_resolve_against_the_request(text: str, back: timedelta) -> None:
    assert time_window._parse_when(text, now=NOW) == NOW - back


def test_absolute_times_still_read_and_nonsense_names_the_forms() -> None:
    assert time_window._parse_when("2026-09-15T14:07:00Z", now=NOW) == datetime(2026, 9, 15, 14, 7)
    assert time_window._parse_when("", now=NOW) is None
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="7 days ago"):
        time_window._parse_when("around lunch", now=NOW)


def test_any_development_branch_reproduces_on_head() -> None:
    assert {"master", "main", "trunk", "develop"} <= DEV_BRANCHES
    assert master_reproduces(["24.8", "main"]) and not master_reproduces(["24.8"])


def _plan(**kw: Any) -> bisect_scan.ScanPlan:
    return bisect_scan.ScanPlan(**kw)


@pytest.mark.asyncio
async def test_no_tracker_bisects_over_the_repositorys_pr_history(monkeypatch) -> None:
    async def history(org: str, ns: str, plan: bisect_scan.ScanPlan, *, limit: int) -> list[dict]:
        return [{"pr_number": 5}]

    monkeypatch.setattr(bisect_scan, "_code_history_rows", history)
    facts = SimpleNamespace(qa_ok=False)
    rows, ok, where = await bisect_scan._candidate_rows(
        None,
        "o",
        "code-x",
        "",
        _plan(paths=["src/a.py"]),
        facts,
        scan=True,
        frames=[],
        patterns=[],
        weights={},
        limit=5,
    )
    assert (rows, ok, where) == ([{"pr_number": 5}], True, "code_history")


@pytest.mark.asyncio
async def test_a_tracker_that_answers_wins_and_one_that_is_empty_falls_back(monkeypatch) -> None:
    answers: list[list[dict]] = [[{"pr_number": 9}], []]

    async def scan(*a: Any, **k: Any) -> tuple[list[dict], bool]:
        return answers.pop(0), True

    async def history(*a: Any, **k: Any) -> list[dict]:
        return [{"pr_number": 5}]

    monkeypatch.setattr(bisect_scan, "_scan", scan)
    monkeypatch.setattr(bisect_scan, "_code_history_rows", history)
    facts = SimpleNamespace(qa_ok=True)
    args = dict(scan=True, frames=[], patterns=[], weights={}, limit=5)
    first = await bisect_scan._candidate_rows(
        object(), "o", "code-x", "t", _plan(dirs=["src/"]), facts, **args
    )
    assert first[2] == "tracker" and first[0] == [{"pr_number": 9}]
    second = await bisect_scan._candidate_rows(
        object(), "o", "code-x", "t", _plan(dirs=["src/"]), facts, **args
    )
    assert second[2] == "code_history"
    # nothing to look for: no history read at all
    none = await bisect_scan._candidate_rows(None, "o", "code-x", "", _plan(), facts, **args)
    assert none == ([], True, "none")


@pytest.mark.asyncio
async def test_the_history_read_matches_paths_and_directories(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Ch:
        async def query_all(self, sql: str, params: dict) -> list[dict]:
            seen.update(params)
            return [
                {
                    "pr_number": 5,
                    "title": "fix parser",
                    "author": "a",
                    "merged_at": NOW,
                    "files": ["src/Parsers/a.cpp", "docs/x.md"],
                    "additions": 3,
                    "deletions": 1,
                }
            ]

    import server.code.domain.index.repos as repos
    import server.core.ch as ch

    monkeypatch.setattr(ch, "get_client", lambda: _Ch())
    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: SimpleNamespace(repo="Acme/w"))
    out = await bisect_scan._code_history_rows("o", "code-x", _plan(dirs=["src/Parsers"]), limit=10)
    assert seen["dirs"] == ["src/Parsers/"] and seen["before"] - seen["after"] == timedelta(days=90)
    assert out[0]["matched_paths"] == ["src/Parsers/a.cpp"]
    assert out[0]["url"] == "https://github.com/Acme/w/pull/5"


@pytest.mark.asyncio
async def test_a_failed_history_read_is_no_candidates(monkeypatch) -> None:
    class _Down:
        async def query_all(self, *a: Any, **k: Any) -> list[dict]:
            raise RuntimeError("Code: 60. Unknown table")

    import server.core.ch as ch

    monkeypatch.setattr(ch, "get_client", lambda: _Down())
    assert await bisect_scan._code_history_rows("o", "code-x", _plan(paths=["a.py"]), limit=3) == []


def test_issue_edges_need_the_trackers_own_repositories() -> None:
    import inspect

    src = inspect.getsource(cb)
    assert "ClickHouse/ClickHouse" not in src
