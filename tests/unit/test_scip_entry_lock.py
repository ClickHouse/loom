"""Per-entry compiler-tier locks: a full pass and a delta never work on one
entry at once, other entries go on, and shell and Python lock the same file."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index import compiler_tier
from server.code.app.index.entry_lock import EntryLockHeld, entry_lock, entry_lock_path
from tests.unit.code_fixtures import repo_config

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _lock_dir(monkeypatch, tmp_path):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_lock_dir", str(tmp_path / "locks"))


def _Cfg(namespace: str) -> Any:
    return repo_config(org="acme", namespace=namespace, scip_producer="clang")


def _hold(path: Path) -> int:
    """Take the lock through a separate open file, as another process would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_lock_path_is_one_file_per_entry(tmp_path) -> None:
    p = entry_lock_path("acme", "code-web")
    assert p == tmp_path / "locks" / "scip.acme.code-web.lock"
    # Dots and slashes are encoded, so no two entries share a file.
    assert entry_lock_path("a.b", "c") != entry_lock_path("a", "b.c")
    assert "/" not in entry_lock_path("acme", "x/y").name


@pytest.mark.asyncio
async def test_non_blocking_raises_when_held_and_releases_on_exit() -> None:
    fd = _hold(entry_lock_path("acme", "code-web"))
    try:
        with pytest.raises(EntryLockHeld):
            async with entry_lock("acme", "code-web", wait=False):
                pass
        # A different entry is free.
        async with entry_lock("acme", "code-api", wait=False):
            pass
    finally:
        os.close(fd)
    async with entry_lock("acme", "code-web", wait=False) as path:
        assert path.exists()
    async with entry_lock("acme", "code-web", wait=False):
        pass  # released by the previous block


@pytest.mark.asyncio
async def test_run_pass_skips_the_held_entry_and_runs_the_others() -> None:
    from server.code.workers import scip_delta as w

    fd = _hold(entry_lock_path("acme", "code-busy"))
    run_repo = AsyncMock()
    try:
        with (
            patch("server.code.store.index.CodeRepo", return_value=AsyncMock()),
            patch(
                "server.code.domain.index.repos.configured_repos",
                return_value=[_Cfg("code-busy"), _Cfg("code-free")],
            ),
            patch.object(compiler_tier, "worker_owns", AsyncMock(return_value=True)),
            patch.object(w, "_run_repo", run_repo),
        ):
            res = await w.run_pass()
    finally:
        os.close(fd)
    assert res.repos_checked == 2
    assert res.skipped == ["code-busy: entry lock held (full pass running)"]
    assert [c.args[1].namespace for c in run_repo.await_args_list] == ["code-free"]
    assert not res.errors


@pytest.mark.asyncio
async def test_run_pass_holds_the_entry_lock_while_it_works() -> None:
    from server.code.workers import scip_delta as w

    seen: list[bool] = []

    async def run_repo(repo, cfg, result):
        try:
            async with entry_lock(cfg.org, cfg.namespace, wait=False):
                seen.append(False)
        except EntryLockHeld:
            seen.append(True)

    with (
        patch("server.code.store.index.CodeRepo", return_value=AsyncMock()),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_Cfg("code-web")],
        ),
        patch.object(compiler_tier, "worker_owns", AsyncMock(return_value=True)),
        patch.object(w, "_run_repo", run_repo),
    ):
        await w.run_pass()
    assert seen == [True]


@pytest.mark.asyncio
async def test_scip_full_waits_for_a_delta_on_its_entry() -> None:
    from server.cli import code_scip

    fd = _hold(entry_lock_path("acme", "code-web"))
    locked = AsyncMock(return_value=0)
    args = argparse.Namespace(org="acme", namespace="code-web")
    with (
        patch("server.core.ch.get_client", return_value=AsyncMock()),
        patch.object(code_scip, "_repo_cfg", return_value=_Cfg("code-web")),
        patch.object(code_scip, "_scip_full_locked", locked),
    ):
        task = asyncio.create_task(code_scip._scip_full(args))
        await asyncio.sleep(0.3)
        assert not locked.await_count, "the full pass ran while a delta held its entry"
        os.close(fd)
        assert await asyncio.wait_for(task, timeout=10) == 0
    assert locked.await_count == 1


@pytest.mark.asyncio
async def test_scip_full_holds_the_lock_for_the_whole_entry() -> None:
    from server.cli import code_scip

    async def locked(args, org, ns, cfg):
        with pytest.raises(EntryLockHeld):
            async with entry_lock(org, ns, wait=False):
                pass
        return 0

    args = argparse.Namespace(org="acme", namespace="code-web")
    with (
        patch("server.core.ch.get_client", return_value=AsyncMock()),
        patch.object(code_scip, "_repo_cfg", return_value=_Cfg("code-web")),
        patch.object(code_scip, "_scip_full_locked", locked),
    ):
        assert await code_scip._scip_full(args) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer", "extra"),
    [("go", ()), ("typescript", ()), ("python", ("typescript",))],
)
async def test_scip_full_skips_an_entry_another_tier_owns(producer, extra, capsys) -> None:

    from server.cli import code_scip

    cfg = repo_config(
        org="acme", namespace="code-web", scip_producer=producer, scip_extra_producers=extra
    )
    locked = AsyncMock(return_value=0)
    args = argparse.Namespace(org="acme", namespace="code-web")
    with (
        patch("server.core.ch.get_client", return_value=AsyncMock()),
        patch.object(code_scip, "_repo_cfg", return_value=cfg),
        patch.object(code_scip, "_scip_full_locked", locked),
    ):
        assert await code_scip._scip_full(args) == 0
    assert not locked.await_count, "scip-clang ran on an entry another compiler tier owns"
    assert f"its compiler tier is {producer}" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_scip_full_runs_a_clang_entry() -> None:

    from server.cli import code_scip

    cfg = repo_config(org="acme", namespace="code-web", scip_producer="clang")
    locked = AsyncMock(return_value=0)
    args = argparse.Namespace(org="acme", namespace="code-web")
    with (
        patch("server.core.ch.get_client", return_value=AsyncMock()),
        patch.object(code_scip, "_repo_cfg", return_value=cfg),
        patch.object(code_scip, "_scip_full_locked", locked),
    ):
        assert await code_scip._scip_full(args) == 0
    assert locked.await_count == 1


def test_cli_prints_the_path_the_workers_lock(capsys) -> None:
    from server.cli import code

    args = argparse.Namespace(org="acme", namespace="code-web")
    assert code._print_scip_lock_path(args) == 0
    assert capsys.readouterr().out.strip() == str(entry_lock_path("acme", "code-web").resolve())


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) not installed")
def test_shell_flock_and_python_exclude_each_other() -> None:
    import time

    from server.code.app.index.entry_lock import _acquire

    path = entry_lock_path("acme", "code-web")
    fd = _acquire(path, wait=False)
    assert fd is not None
    try:
        held = subprocess.run(["flock", "-n", str(path), "true"], check=False)
        assert held.returncode == 1
    finally:
        os.close(fd)
    free = subprocess.run(["flock", "-n", str(path), "true"], check=False)
    assert free.returncode == 0
    proc = subprocess.Popen(["flock", "-n", str(path), "sleep", "30"])
    try:
        # wait until the shell side holds it: a fixed sleep raced a slow spawn
        deadline = time.monotonic() + 10.0
        while subprocess.run(["flock", "-n", str(path), "true"], check=False).returncode == 0:
            assert proc.poll() is None, "flock exited before taking the lock"
            assert time.monotonic() < deadline, "the shell never took the lock"
            time.sleep(0.01)
        assert _acquire(path, wait=False) is None
    finally:
        proc.kill()
        proc.wait()


def test_cron_scripts_use_pass_locks_and_the_shared_entry_lock() -> None:
    delta = (ROOT / "deploy/cron/scip-delta.sh").read_text()
    full = (ROOT / "deploy/cron/scip-full.sh").read_text()
    # No lock shared between the two passes as a whole.
    assert "scip.lock" not in delta and "scip.lock" not in full
    assert "scip-delta.lock" in delta and "scip-full.lock" in full
    # The delta's shell steps lock the path `loom code entries` resolves, non-blocking.
    assert "server.cli code entries" in delta and "server.cli code entries" in full
    assert "flock -n 8" in delta and '8>"$LOCK"' in delta
    assert "nice -n 10" in full
