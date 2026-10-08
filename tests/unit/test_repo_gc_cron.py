"""deploy/cron/repo-gc.sh: git's auto-gc is off in every repository under
var/code_repos, a repack runs only with room on the disk, and a checkout a
scip pass holds is skipped."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "cron" / "repo-gc.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "flock", "timeout", "git", "ionice")),
    reason="needs bash, flock, timeout, git and ionice",
)


def _git(*args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=env
    ).stdout


def _root(tmp_path: Path, lock: Path | None = None) -> Path:
    root = tmp_path / "loom"
    (root / "var" / "cron").mkdir(parents=True)
    repos = root / "var" / "code_repos"
    repos.mkdir()
    _git("init", "-q", str(repos / "checkout"))
    _git("-C", str(repos / "checkout"), "commit", "-q", "--allow-empty", "-m", "x")
    _git("clone", "-q", "--bare", str(repos / "checkout"), str(repos / "mirror.git"))
    py = root / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    entry = f"acme code-a var/code_repos/checkout {lock or root / 'var/cron/none.lock'}"
    py.write_text(f"#!/usr/bin/env bash\necho '{entry}'\n")
    py.chmod(0o755)
    return root


def _run(root: Path, **env: str) -> list[str]:
    subprocess.run(
        ["bash", str(SCRIPT)], check=True, env={**os.environ, "LOOM_ROOT": str(root), **env}
    )
    log = (root / "var" / "cron" / "repo-gc.log").read_text()
    return [line for line in log.splitlines() if line.startswith("STEP")]


def test_auto_gc_is_off_and_each_repository_is_repacked(tmp_path: Path) -> None:
    root = _root(tmp_path)
    steps = _run(root, REPO_GC_MARGIN_GB="0")
    for name in ("checkout", "mirror.git"):
        repo = str(root / "var" / "code_repos" / name)
        assert _git("-C", repo, "config", "gc.auto").strip() == "0"
        assert _git("-C", repo, "config", "maintenance.auto").strip() == "false"
        assert any(f"path=var/code_repos/{name} exit=0" in s and "reason" not in s for s in steps)


def test_no_repack_without_room_on_the_disk(tmp_path: Path) -> None:
    steps = _run(_root(tmp_path), REPO_GC_MARGIN_GB="100000000")
    assert steps and all("reason=low-disk" in s for s in steps)


def test_a_checkout_whose_entry_lock_is_held_is_skipped(tmp_path: Path) -> None:
    lock = tmp_path / "entry.lock"
    root = _root(tmp_path, lock)
    with open(lock, "w") as held:
        holder = subprocess.Popen(["flock", str(lock), "sleep", "30"], stdout=held)
        try:
            subprocess.run(
                ["bash", "-c", f"until ! flock -n {lock} true; do sleep 0.05; done"],
                check=True,
                timeout=10,
            )
            steps = _run(root, REPO_GC_MARGIN_GB="0")
        finally:
            holder.kill()
            holder.wait()
    assert any("path=var/code_repos/checkout" in s and "entry-lock-held" in s for s in steps)
    assert any("path=var/code_repos/mirror.git exit=0" in s for s in steps)


def test_what_the_repack_writes_keeps_the_git_dirs_group(tmp_path: Path) -> None:
    """The container shares the repositories through their group; a repack run as
    the host user must not leave files in that user's primary group."""
    others = [g for g in os.getgroups() if g != os.getgid()]
    if not others:
        pytest.skip("needs a secondary group to tell the two apart")
    shared = others[0]
    root = _root(tmp_path)
    repo = root / "var" / "code_repos" / "mirror.git"
    subprocess.run(["chgrp", "-R", str(shared), str(repo)], check=True)
    _run(root, REPO_GC_MARGIN_GB="0")
    assert _git("-C", str(repo), "config", "core.sharedRepository").strip() == "group"
    stray = [p for p in repo.rglob("*") if p.stat().st_gid != shared]
    assert not stray, stray[:5]


def test_a_repository_another_one_borrows_from_never_prunes(tmp_path: Path) -> None:
    """A checkout whose objects/info/alternates names the mirror keeps only what the
    mirror lacks; a prune in the mirror could delete its only copy."""
    root = _root(tmp_path)
    repos = root / "var" / "code_repos"
    alt = repos / "checkout" / ".git" / "objects" / "info" / "alternates"
    alt.parent.mkdir(parents=True, exist_ok=True)
    alt.write_text("../../../mirror.git/objects\n")
    _run(root, REPO_GC_MARGIN_GB="0")
    assert _git("-C", str(repos / "mirror.git"), "config", "gc.pruneExpire").strip() == "never"
    lone = subprocess.run(
        ["git", "-C", str(repos / "checkout"), "config", "gc.pruneExpire"],
        capture_output=True,
        text=True,
    )
    assert lone.stdout.strip() == ""  # nothing borrows from the checkout
