"""A merged PR's brief is stored only when its diff was read: one built where
the mirror is missing (a read-replica) or git failed would otherwise be served
from the shared table as `precomputed` by every later read."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import gitrepo
from server.code.app.index import review_brief_build as rbb


class _Repo:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[Any]]] = []

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return "head"

    async def insert_rows(self, table: str, rows: list[Any]) -> None:
        self.inserted.append((table, rows))


def _build(monkeypatch: pytest.MonkeyPatch, *, git_ok: bool, changed: list[str]) -> _Repo:
    async def run(git_dir: str, *args: str) -> str:
        if not git_ok:
            raise gitrepo.GitError("fatal: not a git repository")
        return "parent\n" if args[0] == "rev-parse" else "diff --git a/x b/x\n"

    async def build_brief(inp: Any, **kw: Any) -> dict[str, Any]:
        return {"pr_number": inp.pr_number, "merge_sha": inp.merge_sha, "sections": {}}

    async def author(*a: Any) -> str:
        return ""

    monkeypatch.setattr(gitrepo, "run", run)
    monkeypatch.setattr(rbb, "build_brief", build_brief)
    monkeypatch.setattr(rbb, "_ledger_author", author)
    monkeypatch.setattr(rbb, "brief_row", lambda org, ns, payload: payload)
    repo = _Repo()

    async def call(*a: Any, **kw: Any) -> Any:
        return None

    asyncio.run(
        rbb.build_for_merge(
            repo,
            "acme",
            "code-widgets",
            git_dir="/nonexistent",
            pr_number=7,
            title="t",
            merge_sha="m" * 40,
            changed_paths=changed,
            call=call,
        )
    )
    return repo


def test_a_brief_without_its_diff_is_served_unstored(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _build(monkeypatch, git_ok=False, changed=["src/a.cpp"]).inserted == []


def test_a_brief_with_its_diff_is_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    [(table, _)] = _build(monkeypatch, git_ok=True, changed=["src/a.cpp"]).inserted
    assert table == "code_review_briefs"


def test_a_pr_that_changed_no_file_is_stored_without_a_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert len(_build(monkeypatch, git_ok=False, changed=[]).inserted) == 1
