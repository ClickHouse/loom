"""A compiler-tier harness is a bash script whose children do the work
(scip-clang, node, scip-python). Cancelling the pass kills the harness's
whole process group, so no child keeps running in the checkout."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.workers import scip_delta as code_scip_delta
from server.code.workers import scip_go_delta as code_scip_go_delta
from server.code.workers import scip_py_delta as code_scip_py_delta
from server.code.workers import scip_ts_delta as code_scip_ts_delta
from tests.unit.code_fixtures import repo_config

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed orphan can sit as a zombie until its new parent reaps it.
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(stat.stdout.strip()) and not stat.stdout.strip().startswith("Z")


class _Repo:
    async def get_state(self, org: str, ns: str, key: str) -> str:
        return {"git_head": "b" * 40, "scip_commit": "a" * 40}[key]


def _cpp(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    clang = tmp / "scip-clang"
    clang.write_text("")
    monkeypatch.setattr(code_scip_delta.settings, "code_scip_clang_path", str(clang))

    async def _no_git(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(code_scip_delta, "git", _no_git)
    cfg = repo_config(org="o", namespace="ns", scip_checkout_dir=str(tmp), git_dir=str(tmp))
    repo: Any = _Repo()
    return lambda: code_scip_delta._run_repo(repo, cfg, code_scip_delta.CodeScipDeltaResult())


def _go(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    monkeypatch.setattr(code_scip_go_delta, "_harness_env", lambda *a: {})
    monkeypatch.setattr(code_scip_go_delta.settings, "code_scip_go_modcache", str(tmp / "gomod"))
    cfg: Any = SimpleNamespace(org="o", namespace="ns")
    return lambda: code_scip_go_delta._run_harness(tmp, "a", "b", tmp, [], cfg)


def _py(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    monkeypatch.setattr(code_scip_py_delta, "_harness_env", lambda *a: {})
    return lambda: code_scip_py_delta._run_harness(tmp, "a", "b", tmp / "d.json")


def _ts(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    monkeypatch.setattr(code_scip_ts_delta, "_harness_env", lambda *a: {})
    return lambda: code_scip_ts_delta._run_harness(tmp, "a", "b", tmp / "d.json")


_HARNESSES = {"cpp": _cpp, "go": _go, "py": _py, "ts": _ts}


@pytest.mark.parametrize("name", sorted(_HARNESSES))
async def test_cancelling_the_pass_kills_the_harness_and_its_children(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = _HARNESSES[name](tmp_path, monkeypatch)
    pidfile = tmp_path / "grandchild.pid"
    real = asyncio.create_subprocess_exec
    procs: list[asyncio.subprocess.Process] = []

    async def _harness(*_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        # bash that starts a long-running child and waits on it, as a harness
        # waits on its indexer.
        kwargs.pop("env", None)
        script = f'sleep 60 & echo $! > "{pidfile}"; wait'
        proc = await real("bash", "-c", script, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _harness)
    task = asyncio.create_task(call())
    async with asyncio.timeout(10):
        while not (pidfile.exists() and pidfile.read_text().strip()):  # noqa: ASYNC110 — another process writes it
            await asyncio.sleep(0.02)
    grandchild = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(procs) == 1
    try:
        async with asyncio.timeout(5):
            await procs[0].wait()
            while _alive(grandchild):  # noqa: ASYNC110 — polls another process
                await asyncio.sleep(0.05)
    finally:
        for pid in (procs[0].pid, grandchild):
            if _alive(pid):
                os.kill(pid, 9)
    assert not _alive(procs[0].pid)
    assert not _alive(grandchild)
