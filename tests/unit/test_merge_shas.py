"""Merge commit shas on blame_range / bisect candidates: stored first, else one
first-parent search of the mirror, else "" — against a tiny git repo built here."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.code.app import bisect as hb
from server.code.app import blame as hc
from server.code.app.index import gitrepo
from server.code.app.index import merge_shas as ms
from server.code.domain.index import blame_range as br
from server.tracker.domain import bisect as bi

T0 = 1_700_000_000


def _git(repo: Path, *args: str, when: int = 0) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_AUTHOR_DATE": f"{T0 + when} +0000",
        "GIT_COMMITTER_DATE": f"{T0 + when} +0000",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _commit(repo: Path, name: str, msg: str, when: int) -> str:
    (repo / name).write_text(msg + "\n")
    _git(repo, "add", name, when=when)
    _git(repo, "commit", "-qm", msg, when=when)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def mirror(tmp_path: Path) -> dict[str, str]:
    """master: base → `Merge pull request #10` (a side-branch commit) → squash `(#12)` →
    a commit that only mentions #13 → `Backport #14 to 26.3`."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _commit(repo, "a", "base", 0)
    _git(repo, "checkout", "-qb", "feature")
    side = _commit(repo, "b", "Initial implementation", 10)
    _git(repo, "checkout", "-q", "master")
    _git(
        repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #10 from o/f", when=20
    )
    merge = _git(repo, "rev-parse", "HEAD")
    squash = _commit(repo, "c", "Tune the reader (#12)", 30)
    _commit(repo, "d", "Follow-up to #13, no PR of its own", 40)
    _commit(repo, "e", "Backport #14 to 26.3", 50)
    return {"git_dir": str(repo / ".git"), "merge": merge, "squash": squash, "side": side}


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = gitrepo.run

    async def counted(git_dir: str, *args: str, timeout_s: float = 20.0) -> str:
        calls.append(args)
        return await real(git_dir, *args, timeout_s=timeout_s)

    monkeypatch.setattr(gitrepo, "run", counted)
    return calls


def test_landed_pr_of_takes_merges_and_squashes_only() -> None:
    assert br.landed_pr_of("Merge pull request #10 from o/f") == 10
    assert br.landed_pr_of("Tune the reader (#12)") == 12
    assert br.landed_pr_of("Follow-up to #13") == 0
    assert br.landed_pr_of("Backport #14 to 26.3") == 0


def test_parse_landed_keeps_the_newest_landing() -> None:
    a, b = "a" * 40, "b" * 40
    text = f"{a}\tReland x (#7)\n{b}\tx (#7)\nnot a line\n{'c' * 40}\tOther (#8)\n"
    assert ms.parse_landed(text, {7}) == {7: a}


@pytest.mark.asyncio
async def test_stored_mirror_merge_squash_and_not_found(mirror, git_calls) -> None:
    stored = {99: ("f" * 40).encode()}  # FixedString comes back as bytes
    got = await ms.resolve(mirror["git_dir"], "refs/heads/master", [10, 12, 13, 14, 99], stored)
    assert got[99] == ("f" * 40, ms.SOURCE_STORED)
    assert got[10] == (mirror["merge"], ms.SOURCE_MIRROR)
    assert got[12] == (mirror["squash"], ms.SOURCE_MIRROR)
    assert got[13] == ("", "") and got[14] == ("", "")
    assert len(git_calls) == 1  # every missing PR in one search
    assert "--first-parent" in git_calls[0]


@pytest.mark.asyncio
async def test_all_stored_runs_no_git(mirror, git_calls) -> None:
    got = await ms.resolve(mirror["git_dir"], "refs/heads/master", [10], {10: "e" * 40})
    assert got == {10: ("e" * 40, "stored")} and git_calls == []


@pytest.mark.asyncio
async def test_since_bounds_the_search(mirror) -> None:
    # every landing is older than the window's oldest candidate minus the slack
    late = T0 + 30 * 86400
    got = await ms.resolve(mirror["git_dir"], "refs/heads/master", [10], {}, since_unix=late)
    assert got == {10: ("", "")}


@pytest.mark.asyncio
async def test_git_failure_is_empty_not_raised(tmp_path: Path) -> None:
    got = await ms.resolve(str(tmp_path / "none.git"), "refs/heads/master", [10], {})
    assert got == {10: ("", "")}


@pytest.mark.asyncio
async def test_blame_fill_leaves_known_shas_and_fills_the_rest(mirror, git_calls) -> None:
    known = br.Candidate(pr_number=5, merge_commit_sha="d" * 40, merge_sha_source="stored")
    ledger_only = br.Candidate(pr_number=12, merged_at="2023-11-14T22:13:50+00:00")
    gone = br.Candidate(pr_number=77)
    await hc.fill_merge_shas(mirror["git_dir"], "master", [known, ledger_only, gone])
    assert (known.merge_commit_sha, known.merge_sha_source) == ("d" * 40, "stored")
    assert ledger_only.as_dict()["merge_commit_sha"] == mirror["squash"]
    assert ledger_only.as_dict()["merge_sha_source"] == "mirror"
    assert (gone.merge_commit_sha, gone.merge_sha_source) == ("", "")
    assert len(git_calls) == 1


def test_merge_legs_names_the_source() -> None:
    att = br.Attribution(lines={3: 2}, merge_sha={3: "a" * 40})
    rows: list[dict[str, Any]] = [
        {"pr_number": 1, "merge_commit_sha": "b" * 40, "merged_at": "2026-01-01T00:00:00"},
        {"pr_number": 2, "merge_commit_sha": "", "merged_at": "2026-01-02T00:00:00"},
    ]
    got = {
        c.pr_number: (c.merge_commit_sha, c.merge_sha_source)
        for c in br.merge_legs(att, rows, range_len=4, branch_points=[])
    }
    assert got == {1: ("b" * 40, "stored"), 2: ("", ""), 3: ("a" * 40, "mirror")}


class _Cfg:
    def __init__(self, git_dir: str) -> None:
        self.git_dir, self.git_branch = git_dir, "master"


class _Repo:
    def __init__(self, stored: dict[int, str]) -> None:
        self.stored = stored

    async def merge_commits_of(self, org: str, ns: str, numbers: list[int]) -> dict[int, str]:
        return {n: s for n, s in self.stored.items() if n in numbers}


def _cand(n: int) -> bi.Candidate:
    return bi.Candidate(
        pr_number=n, title="t", author="a", merged_at="2023-11-14 22:13:50", minor_version=""
    )


@pytest.mark.asyncio
async def test_bisect_candidates_carry_merge_shas(mirror, git_calls, monkeypatch) -> None:
    monkeypatch.setattr(hb.cb, "repo_siblings", lambda org, ns: (_Cfg(mirror["git_dir"]), []))
    ranked = [_cand(10), _cand(12), _cand(13), _cand(40)]
    await hb._attach_merge_shas(_Repo({40: "c" * 40}), "o", "n", ranked)
    out = {
        c["pr_number"]: (c["merge_commit_sha"], c["merge_sha_source"])
        for c in (r.as_dict() for r in ranked)
    }
    assert out == {
        10: (mirror["merge"], "mirror"),
        12: (mirror["squash"], "mirror"),
        13: ("", ""),
        40: ("c" * 40, "stored"),
    }
    assert len(git_calls) == 1
    assert git_calls[0][-2:] == ("refs/heads/master", "--")
