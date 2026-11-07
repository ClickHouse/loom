"""tests/unit/git_repo.py: git that signs nothing and runs no hooks, whatever the
repository's config asks."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit import git_repo


def test_a_commit_and_a_tag_need_no_key_and_run_no_hook(tmp_path: Path) -> None:
    git_repo.git(tmp_path, "init", "-q")
    for key in ("commit.gpgsign", "tag.gpgSign"):
        git_repo.git(tmp_path, "config", key, "true")
    git_repo.git(tmp_path, "config", "gpg.program", "false")  # a signature would fail
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "commit-msg").write_text("#!/bin/sh\nexit 1\n")  # a hook would fail
    (hooks / "commit-msg").chmod(0o755)
    git_repo.git(tmp_path, "config", "core.hooksPath", str(hooks))
    git_repo.git(tmp_path, "commit", "-q", "--allow-empty", "-m", "x")
    git_repo.git(tmp_path, "tag", "v1.0.0")
    # A plain tag, not a signed tag object.
    assert git_repo.git(tmp_path, "cat-file", "-t", "v1.0.0").strip() == "commit"


def test_a_failure_says_what_git_said(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="^git tag v1 failed: fatal: "):
        git_repo.git(tmp_path, "tag", "v1")  # not a repository
