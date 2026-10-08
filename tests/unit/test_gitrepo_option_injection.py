"""A stored watermark or a caller's commit never reaches git as an option.

The range helpers build `since..until` from ClickHouse state and from
request input (`index_status(commit=)`); a value starting with `-` would be
parsed by git as an option (`--output=<path>` writes a file). The helpers
refuse such a value before a git process starts."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from server.code.app.index import gitrepo

EVIL = "--output=/dev/null"
GitErrorOrValue = (gitrepo.GitError, ValueError)


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    spawned: list[tuple] = []

    async def spawn(*args, **kw):
        spawned.append(args)
        raise AssertionError(f"git ran: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return spawned


@pytest.mark.parametrize(
    "call",
    [
        lambda: gitrepo.commits_between("/g", EVIL, "a" * 40, 10),
        lambda: gitrepo.commits_between("/g", "a" * 40, EVIL, 10),
        lambda: gitrepo.commits_between("/g", "main", "a" * 40, 10),
        lambda: gitrepo.commit_count_between("/g", EVIL, "a" * 40),
        lambda: gitrepo.commit_count_between("/g", "a" * 40, "HEAD~1"),
        lambda: gitrepo.diff_files("/g", EVIL, "a" * 40),
        lambda: gitrepo.diff_name_only("/g", "a" * 40, EVIL),
        lambda: gitrepo.commit_subject("/g", EVIL),
        lambda: gitrepo.commit_time("/g", EVIL),
        lambda: gitrepo.ls_tree("/g", EVIL),
        lambda: gitrepo.ls_tree_blobs("/g", EVIL),
        lambda: gitrepo.branches_containing("/g", EVIL),
        lambda: gitrepo.release_containing("/g", EVIL),
        lambda: gitrepo.grep_paths("/g", EVIL, "x", ()),
    ],
)
def test_a_non_revision_is_refused_before_git_runs(no_git, call) -> None:
    with pytest.raises(GitErrorOrValue):
        asyncio.run(call())
    assert no_git == []


def test_readers_with_a_not_found_answer_give_it_for_a_non_revision(no_git) -> None:
    assert asyncio.run(gitrepo.is_ancestor("/g", EVIL, "a" * 40)) is None
    assert asyncio.run(gitrepo.show_file("/g", EVIL, "README")) is None
    assert asyncio.run(gitrepo.merge_base("/g", "a" * 40, EVIL)) == ""
    with pytest.raises(ValueError):
        asyncio.run(gitrepo.resolve_commit("/g", EVIL))
    assert no_git == []


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_object_ids_still_count_against_a_real_repository(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    shas = []
    for i in range(3):
        (repo / "f").write_text(str(i))
        _git(repo, "add", "f")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", str(i))
        shas.append(_git(repo, "rev-parse", "HEAD"))
    git_dir = str(repo / ".git")
    assert asyncio.run(gitrepo.commit_count_between(git_dir, shas[0], shas[2])) == 2
    assert asyncio.run(gitrepo.commit_count_between(git_dir, shas[0][:7].upper(), shas[2])) == 2
    assert asyncio.run(gitrepo.commits_between(git_dir, shas[0], shas[2], 10)) == shas[1:]
    probe = tmp_path / "written-by-git"
    with pytest.raises(gitrepo.GitError):
        asyncio.run(gitrepo.commits_between(git_dir, f"--output={probe}", shas[2], 10))
    assert not probe.exists()
