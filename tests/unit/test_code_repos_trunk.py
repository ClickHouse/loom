"""A release-branch entry reads its repository's trunk through one resolver: the entry
on the same mirror whose branch is `master` or `main`, whatever branch the scalar
settings name."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app import blame, citations
from server.code.domain.index.repos import trunk_entry
from server.settings import settings
from tests.unit.code_fixtures import repo_config

MIRROR = "var/code_repos/app.git"


def _entries() -> list[Any]:
    return [
        repo_config(
            org="o", namespace="code-app", repo="Acme/app", git_dir=MIRROR, git_branch="main"
        ),
        repo_config(
            org="o", namespace="code-app-2-1", repo="Acme/app", git_dir=MIRROR, git_branch="2.1"
        ),
        repo_config(
            org="o",
            namespace="code-other",
            repo="Acme/other",
            git_dir="var/o.git",
            git_branch="2.1",
        ),
    ]


def test_trunk_entry_is_the_main_branch_on_the_same_mirror() -> None:
    trunk, release, other = _entries()
    entries = [trunk, release, other]
    assert trunk_entry(release, entries) is trunk
    assert trunk_entry(trunk, entries) is None  # a trunk has no trunk
    assert trunk_entry(other, entries) is None  # no trunk configured on its mirror


def test_citations_read_the_trunk_of_a_main_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_git_branch", "master")  # the scalar entry's branch
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", _entries)
    assert citations._namespaces("o", "code-app-2-1", "") == ["code-app-2-1", "code-app"]
    assert citations._namespaces("o", "code-app", "") == ["code-app"]
    assert citations._namespaces("o", "code-app-2-1", "code-x") == ["code-app-2-1", "code-x"]


def test_blame_ledger_reads_the_trunk_of_a_main_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_git_branch", "master")
    asked: list[tuple[str, str]] = []

    class _Repo:
        async def pr_history_in_window(self, org: str, ns: str, path: str, **kw: Any) -> list[Any]:
            asked.append((ns, kw["branch"]))
            return []

    siblings = _entries()[:2]
    rows, ok, skipped = asyncio.run(
        blame._ledger_leg(_Repo(), "o", "code-app-2-1", siblings, "src/a.py", blame.Window())  # type: ignore[arg-type]
    )
    assert ok and rows == [] and skipped == []
    assert asked == [("code-app-2-1", "2.1"), ("code-app", "main")]
