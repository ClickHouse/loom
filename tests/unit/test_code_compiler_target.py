"""A compiler tier follows the branch head in the entry's mirror, not the
parser tier's `git_head` watermark: a parser backlog does not hold the
compiled index back. Before the first index it waits; a mirror that cannot
answer falls back to `git_head`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.code.app.index.compiler_tier import compiler_target
from server.code.workers import scip_py_delta as py_w
from tests.unit.code_fixtures import repo_config


def _mirror(tmp_path: Path) -> tuple[Path, str, str]:
    """A bare mirror whose `main` is two commits ahead of the first one."""
    work = tmp_path / "work"
    run = lambda *a, cwd=work: subprocess.run(  # noqa: E731
        ["git", *a], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()
    work.mkdir()
    run("init", "-q", "-b", "main")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one")
    first = run("rev-parse", "HEAD")
    for m in ("two", "three"):
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", m)
    head = run("rev-parse", "HEAD")
    bare = tmp_path / "mirror.git"
    run("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    return bare, first, head


def _repo(git_head: str, **state: str) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return {"git_head": git_head, **state}.get(key, "")

    repo.get_state.side_effect = get_state
    repo.live_scip_paths.return_value = set()
    return repo


def _Cfg(git_dir: str) -> Any:
    return repo_config(
        org="org", namespace="code-py", scip_producer="python", git_branch="main", git_dir=git_dir
    )


@pytest.mark.asyncio
async def test_target_is_the_mirror_head_when_the_parser_tier_lags(tmp_path: Path) -> None:
    bare, first, head = _mirror(tmp_path)
    assert await compiler_target(_repo(first), _Cfg(str(bare))) == head


@pytest.mark.asyncio
async def test_no_first_index_means_no_target(tmp_path: Path) -> None:
    bare, _, _ = _mirror(tmp_path)
    assert await compiler_target(_repo(""), _Cfg(str(bare))) == ""


@pytest.mark.asyncio
async def test_an_unreadable_mirror_falls_back_to_git_head(tmp_path: Path) -> None:
    assert await compiler_target(_repo("a" * 40), _Cfg(str(tmp_path / "missing.git"))) == "a" * 40


@pytest.mark.asyncio
async def test_the_python_worker_indexes_the_mirror_head(monkeypatch, tmp_path: Path) -> None:
    bare, first, head = _mirror(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(py_w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(py_w.settings, "code_scip_python_path", sys.executable)
    windows: list[tuple[str, str]] = []

    async def fake_git(cwd, *args, **kw):
        return None

    async def fake_harness(co, base, target, delta_path):
        windows.append((base, target))  # no delta file: no Python changes

    monkeypatch.setattr(py_w, "git", fake_git)
    monkeypatch.setattr(py_w, "_run_harness", fake_harness)
    repo = _repo(first, scip_commit_py=first)
    result = py_w.CodeScipPyDeltaResult()
    await py_w._run_repo(repo, _Cfg(str(bare)), result)
    assert windows == [(first, head)]
    repo.set_state.assert_awaited_with("org", "code-py", py_w.WATERMARK, head)
