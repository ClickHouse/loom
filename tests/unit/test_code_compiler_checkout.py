"""Compiler-tier working trees: created when missing, except an operator's scip-clang one."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from server.code.app.index.compiler_tier import resolve_checkout
from tests.unit.code_fixtures import repo_config


def _bare_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "main.py").write_text("print('ok')\n")
    subprocess.run(["git", "-C", str(source), "add", "main.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    bare = tmp_path / "repo.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(bare)], check=True)
    return bare


def test_registry_entry_initializes_checkout_for_its_bare_mirror(tmp_path: Path) -> None:
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkouts" / "code-example"
    cfg = repo_config(
        source="registry",
        git_dir=str(bare),
        scip_checkout_dir=str(checkout),
        classification="standard",
    )

    actual, actual_bare, missing = resolve_checkout(cfg, "python")

    assert missing is None
    assert actual == tmp_path / "checkouts" / "code-example.python"
    assert (actual / ".git").exists()
    assert actual_bare == bare.resolve()


def test_operator_entry_gets_a_checkout_per_tier_scip_clang_included(tmp_path: Path) -> None:
    """A new pod starts with no tree: the C++ tier gets one like every tier."""
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "operator-owned"
    cfg = repo_config(source="env", git_dir=str(bare), scip_checkout_dir=str(checkout))

    actual, _bare, missing = resolve_checkout(cfg, "clang")
    assert missing is None and actual == checkout and (checkout / ".git").exists()

    actual, _bare, missing = resolve_checkout(cfg, "python")
    assert missing is None
    assert actual == tmp_path / "operator-owned.python" and (actual / ".git").exists()


def test_a_checkout_that_cannot_be_created_is_reported_with_the_reason(tmp_path: Path) -> None:
    bare = _bare_repo(tmp_path)
    (tmp_path / "a-file").write_text("")
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "a-file" / "checkout"))

    _actual, _bare, missing = resolve_checkout(cfg, "python")

    assert missing is not None and missing.startswith("checkout missing at ")
    assert "(mkdir: Not a directory)" in missing  # made 0700 before `git init` runs


@pytest.mark.asyncio
async def test_no_git_command_in_a_checkout_starts_a_repack(tmp_path: Path, monkeypatch) -> None:
    """Auto-gc inside a fetch writes a pack as big as the repository; every git
    command a tier runs turns it off, in trees made by any build."""
    from server.code.app.index import compiler_tier
    from server.code.app.index.sandbox import JobResult

    seen: list[list[str]] = []

    async def _job(argv, **kw):
        seen.append(argv)
        return JobResult(0, b"", b"")

    monkeypatch.setattr(compiler_tier, "run_job", _job)
    await compiler_tier.git(tmp_path, "fetch", "origin")
    assert seen[0][3:7] == ["-c", "gc.auto=0", "-c", "maintenance.auto=false"]
