"""`code.index_status(commit=…)` says whether every index
tier has reached a merge commit, per commit, not by lag."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from server.code.app.index import commit_coverage as cc


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[str, list[str]]:
    """A linear history A -> B -> C -> D; returns (git_dir, [A, B, C, D])."""
    _git(tmp_path, "init", "-q", "-b", "master")
    shas = []
    for name in "ABCD":
        (tmp_path / "f").write_text(name)
        _git(tmp_path, "add", "f")
        _git(tmp_path, "commit", "-q", "-m", name)
        shas.append(_git(tmp_path, "rev-parse", "HEAD"))
    cc.reset()
    return str(tmp_path / ".git"), shas


def _tiers(clang: str, py: str) -> list[dict[str, str]]:
    return [
        {"producer": "clang", "label": "C++", "watermark_key": "scip_commit", "commit": clang},
        {"producer": "python", "label": "Python", "watermark_key": "scip_commit_py", "commit": py},
    ]


@pytest.mark.asyncio
async def test_ready_only_when_every_tier_reaches_the_commit(repo: tuple[str, list[str]]) -> None:
    git_dir, (a, b, c, d) = repo
    # fast tier at D, C++ at C, Python at B
    got = await cc.commit_coverage(git_dir, b, d, _tiers(c, b))
    assert got["ready"] and got["waiting_on"] == [] and got["in_mirror"]
    got = await cc.commit_coverage(git_dir, c, d, _tiers(c, b))
    assert not got["ready"] and got["waiting_on"] == ["Python"]
    assert [t["covered"] for t in got["tiers"]] == [True, False]
    got = await cc.commit_coverage(git_dir, d, d, _tiers(c, b))
    assert got["fast"] is True and got["waiting_on"] == ["C++", "Python"]


@pytest.mark.asyncio
async def test_a_commit_ahead_of_the_fast_tier_waits_on_it(repo: tuple[str, list[str]]) -> None:
    git_dir, (a, b, c, d) = repo
    got = await cc.commit_coverage(git_dir, d, c, _tiers(b, a))
    assert got["fast"] is False and not got["ready"]
    assert got["waiting_on"][0] == "fast tier"


@pytest.mark.asyncio
async def test_unknown_or_malformed_commits_are_never_ready(repo: tuple[str, list[str]]) -> None:
    git_dir, (a, b, c, d) = repo
    got = await cc.commit_coverage(git_dir, "f" * 40, d, _tiers(c, b))
    assert not got["ready"] and not got["in_mirror"]
    assert got["waiting_on"] == ["commit not in the index's mirror yet"]
    for bad in ("", "HEAD", "--all", "abc"):
        got = await cc.commit_coverage(git_dir, bad, d, _tiers(c, b))
        assert not got["ready"] and got["waiting_on"] == ["invalid commit"]


@pytest.mark.asyncio
async def test_short_shas_and_unset_tiers(repo: tuple[str, list[str]]) -> None:
    git_dir, (a, b, c, d) = repo
    got = await cc.commit_coverage(git_dir, a[:9], d, _tiers(c, ""))
    # a tier with no watermark yet is not required; the others cover A
    assert got["ready"] and got["tiers"][1]["covered"] is None


def test_the_input_accepts_a_commit() -> None:
    from server.code.api.models_status import CodeIndexStatusInput, CodeIndexStatusOutput

    assert CodeIndexStatusInput(commit="abc1234").commit == "abc1234"
    assert "commit_coverage" in CodeIndexStatusOutput.model_fields


@pytest.fixture()
def repo_files(tmp_path: Path) -> tuple[str, list[str]]:
    """A -> B -> C -> D, each commit touching its own file (a.txt ... d.txt)."""
    _git(tmp_path, "init", "-q", "-b", "master")
    shas = []
    for name in "abcd":
        (tmp_path / f"{name}.txt").write_text(name)
        _git(tmp_path, "add", f"{name}.txt")
        _git(tmp_path, "commit", "-q", "-m", name)
        shas.append(_git(tmp_path, "rev-parse", "HEAD"))
    cc.reset()
    return str(tmp_path / ".git"), shas


@pytest.mark.asyncio
async def test_drift_names_how_far_and_which_paths_moved(repo_files: tuple[str, list[str]]) -> None:
    """Reviewing B while the index is at D, a reviewer learns
    the index is 2 commits past and that c.txt and d.txt changed since."""
    git_dir, (a, b, c, d) = repo_files
    got = await cc.commit_coverage(git_dir, b, d, _tiers(c, b))
    assert got["commits_since"] == 2
    assert sorted(got["paths_changed_since"]) == ["c.txt", "d.txt"]
    assert got["paths_changed_count"] == 2 and got["paths_truncated"] is False
    assert [t["commits_since"] for t in got["tiers"]] == [1, 0]  # C++ at C, Python at B


@pytest.mark.asyncio
async def test_no_drift_when_the_index_is_at_the_commit(repo_files: tuple[str, list[str]]) -> None:
    git_dir, (a, b, c, d) = repo_files
    got = await cc.commit_coverage(git_dir, d, d, _tiers(d, d))
    assert got["commits_since"] == 0 and got["paths_changed_since"] == [] and got["ready"]


@pytest.mark.asyncio
async def test_drift_is_unknown_until_the_fast_tier_covers_the_commit(
    repo_files: tuple[str, list[str]],
) -> None:
    git_dir, (a, b, c, d) = repo_files
    got = await cc.commit_coverage(git_dir, d, c, _tiers(b, a))
    assert got["commits_since"] == -1 and got["paths_changed_count"] == -1
    assert [t["commits_since"] for t in got["tiers"]] == [-1, -1]


@pytest.mark.asyncio
async def test_paths_are_capped(
    monkeypatch: pytest.MonkeyPatch, repo_files: tuple[str, list[str]]
) -> None:
    git_dir, (a, b, c, d) = repo_files
    monkeypatch.setattr(cc, "PATHS_CAP", 1)
    got = await cc.commit_coverage(git_dir, b, d, _tiers(c, b))
    assert (
        len(got["paths_changed_since"]) == 1
        and got["paths_truncated"]
        and got["paths_changed_count"] == 2
    )


@pytest.mark.asyncio
async def test_overlap_names_only_the_reviewers_files_that_moved(
    repo_files: tuple[str, list[str]],
) -> None:
    git_dir, (a, b, c, d) = repo_files
    got = await cc.commit_coverage(git_dir, b, d, _tiers(c, b), paths=["b.txt", "c.txt", "zzz.txt"])
    assert got["paths_changed_overlap"] == ["c.txt"]  # b.txt is the PR's own, untouched since
    got = await cc.commit_coverage(git_dir, b, d, _tiers(c, b), paths=["a.txt"])
    assert got["paths_changed_overlap"] == []
