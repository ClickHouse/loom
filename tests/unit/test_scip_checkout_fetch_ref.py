"""A checkout fetch records its target on a ref, so the next fetch negotiates from
it and sends only what is new."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from server.code.app.index.compiler_tier import SCIP_HEAD_REF, checkout_fetch_args

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=_ENV
    ).stdout.strip()


def _commits(repo: Path, n: int, tag: str) -> str:
    for i in range(n):
        (repo / f"{tag}{i}.txt").write_text(f"{tag} {i}\n" * 50)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"{tag}{i}")
    return _git(repo, "rev-parse", "HEAD")


def _received(checkout: Path, args: list[str]) -> int:
    """Objects the fetch brought in (loose + packed count delta)."""

    def count() -> int:
        out = dict(line.split(": ") for line in _git(checkout, "count-objects", "-v").splitlines())
        return int(out["count"]) + int(out["in-pack"])

    before = count()
    _git(checkout, *args)
    return count() - before


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "master")
    _commits(src, 3, "base")
    bare = tmp_path / "mirror.git"
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    return src, bare, checkout


def test_the_fetched_target_lands_on_the_ref_and_the_next_fetch_sends_only_new(
    tmp_path: Path,
) -> None:
    src, bare, checkout = _setup(tmp_path)
    first = _commits(src, 20, "a")
    _git(src, "push", "-q", str(bare), "master")
    _received(checkout, checkout_fetch_args(bare, first))
    assert _git(checkout, "rev-parse", SCIP_HEAD_REF) == first
    _git(checkout, "checkout", "-q", "--force", first)

    second = _commits(src, 2, "b")
    _git(src, "push", "-q", str(bare), "master")
    got = _received(checkout, checkout_fetch_args(bare, second))
    assert _git(checkout, "rev-parse", SCIP_HEAD_REF) == second
    assert got <= 2 * 3  # two commits: commit + tree + blob each


def _foreign_owner_env(remote: Path) -> dict[str, str]:
    """A repository job's environment, with every repository taken as another uid's."""
    from server.code.app.index.gitrepo import auth_env

    return {**auth_env(remote=str(remote)), "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}


def _require_owner_check(checkout: Path, bare: Path, target: str) -> None:
    """Only some git releases refuse a foreign-owned source in upload-pack; skip where
    this one does not."""
    plain = subprocess.run(
        ["git", "-C", str(checkout), "fetch", str(bare), f"+{target}:refs/plain"],
        capture_output=True,
        text=True,
        env=_foreign_owner_env(bare),
    )
    if plain.returncode == 0:
        pytest.skip("this git's upload-pack does not refuse a foreign-owned source")
    assert "dubious ownership" in plain.stderr


def test_a_bare_clone_another_uid_owns_is_fetched_from(tmp_path: Path) -> None:
    src, bare, checkout = _setup(tmp_path)
    target = _commits(src, 1, "c")
    _git(src, "push", "-q", str(bare), "master")
    _require_owner_check(checkout, bare, target)
    done = subprocess.run(
        ["git", "-C", str(checkout), *checkout_fetch_args(bare, target)],
        capture_output=True,
        text=True,
        env=_foreign_owner_env(bare),
    )
    assert done.returncode == 0, done.stderr
    assert _git(checkout, "rev-parse", SCIP_HEAD_REF) == target


@pytest.mark.asyncio
async def test_a_local_remote_another_uid_owns_is_read(tmp_path: Path, monkeypatch) -> None:
    from server.code.app.index import gitrepo, sandbox

    src, bare, checkout = _setup(tmp_path)
    target = _commits(src, 1, "c")
    _git(src, "push", "-q", str(bare), "master")
    _require_owner_check(checkout, bare, target)
    real = gitrepo.auth_env
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: False)
    monkeypatch.setattr(
        gitrepo,
        "auth_env",
        lambda token="", remote="": {**real(token, remote), "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
    )
    heads = await gitrepo.remote_branches(str(checkout / ".git"), str(bare))
    assert heads["master"] == target
