"""A window whose base commit the checkout no longer has (history rewritten)
cannot be diffed. The harness exits BASE_UNKNOWN_EXIT, and the worker clears
the tier's watermark so the next run indexes in full, instead of retrying
the same window forever. The Go worker's own diff must not read a git
failure as "no Go change" and advance the watermark past the window."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index import compiler_tier as shared
from server.code.workers import scip_go_delta as go_w
from server.code.workers import scip_py_delta as py_w
from tests.unit.code_fixtures import repo_config


def test_exit_3_is_base_unknown_and_other_exits_stay_plain_errors() -> None:
    assert isinstance(shared.harness_failure(shared.BASE_UNKNOWN_EXIT, "gone"), shared.BaseUnknown)
    err = shared.harness_failure(1, "boom")
    assert not isinstance(err, shared.BaseUnknown) and "harness exit 1: boom" in str(err)


def _Cfg(**kw: Any) -> Any:
    return repo_config(**{"org": "org", "namespace": "code-py", "scip_producer": "python", **kw})


def _repo() -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return {"git_head": "b" * 40, "scip_commit_py": "a" * 40}.get(key, "")

    repo.get_state.side_effect = get_state
    return repo


@pytest.mark.asyncio
async def test_the_python_worker_clears_its_watermark_on_base_unknown(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "checkout").mkdir()
    (tmp_path / "repo.git").mkdir()
    monkeypatch.setattr(py_w.settings, "code_scip_checkout_dir", str(tmp_path / "checkout"))
    monkeypatch.setattr(py_w.settings, "code_git_dir", str(tmp_path / "repo.git"))
    monkeypatch.setattr(py_w.settings, "code_scip_python_path", sys.executable)

    async def fake_git(cwd, *args, **kw):
        return None

    async def gone(*a, **kw):
        raise shared.harness_failure(shared.BASE_UNKNOWN_EXIT, "base aaaa not found")

    monkeypatch.setattr(py_w, "git", fake_git)
    monkeypatch.setattr(py_w, "_run_harness", gone)
    repo = _repo()
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch("server.code.domain.index.repos.configured_repos", return_value=[_Cfg()]),
    ):
        res = await py_w.run_pass()
    cleared = [c for c in repo.set_state.await_args_list if c.args[2:4] == (py_w.WATERMARK, "")]
    assert cleared and cleared[0].kwargs["detail"].startswith("base unknown")
    assert any("indexes in full" in e for e in res.errors)
    assert res.deltas_applied == 0


def _checkout(tmp_path: Path) -> tuple[Path, str, str]:
    co = tmp_path / "co"
    co.mkdir()

    def run(*a: str) -> str:
        return subprocess.run(
            ["git", *a], cwd=co, check=True, capture_output=True, text=True
        ).stdout.strip()

    run("init", "-q", "-b", "main")
    (co / "a.go").write_text("package a\n")
    run("add", "a.go")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "one")
    first = run("rev-parse", "HEAD")
    (co / "b.go").write_text("package a\n")
    run("add", "b.go")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "two")
    return co, first, run("rev-parse", "HEAD")


def test_go_changed_paths_reads_the_window_and_names_an_unknown_base(tmp_path: Path) -> None:
    co, first, head = _checkout(tmp_path)
    assert asyncio.run(go_w._changed_paths(co, first, head)) == ["b.go"]
    with pytest.raises(shared.BaseUnknown):
        asyncio.run(go_w._changed_paths(co, "c" * 40, head))


@pytest.mark.asyncio
async def test_a_go_window_with_an_unknown_base_never_advances_the_watermark(
    monkeypatch, tmp_path: Path
) -> None:
    co, _first, head = _checkout(tmp_path)

    async def fake_git(cwd, *args, **kw):
        return None

    monkeypatch.setattr(go_w, "git", fake_git)
    monkeypatch.setattr(go_w, "_resolve_paths", lambda cfg: (co, tmp_path, None))
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return {"git_head": head, go_w.WATERMARK: "d" * 40}.get(key, "")

    repo.get_state.side_effect = get_state

    with pytest.raises(shared.BaseUnknown):
        await go_w._run_repo(
            repo, _Cfg(namespace="code-go", scip_producer="go"), go_w.CodeScipGoDeltaResult()
        )
    assert not any(c.args[2:4] == (go_w.WATERMARK, head) for c in repo.set_state.await_args_list)
