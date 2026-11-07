"""tests/unit/git_repo.py: git that signs nothing, whatever the repository's config asks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit import git_repo


def test_a_commit_and_a_tag_need_no_key_where_the_config_asks_to_sign(tmp_path: Path) -> None:
    git_repo.git(tmp_path, "init", "-q")
    for key in ("commit.gpgsign", "tag.gpgSign"):
        git_repo.git(tmp_path, "config", key, "true")
    git_repo.git(tmp_path, "config", "gpg.program", "false")  # a signature would fail
    git_repo.git(tmp_path, "commit", "-q", "--allow-empty", "-m", "x")
    git_repo.git(tmp_path, "tag", "v1.0.0")
    tagged = subprocess.run(
        ["git", "cat-file", "-t", "v1.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tagged.stdout.strip() == "commit"  # a plain tag, not a signed tag object


def test_a_failure_says_what_git_said(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="^git tag v1 failed: fatal: "):
        git_repo.git(tmp_path, "tag", "v1")  # not a repository
