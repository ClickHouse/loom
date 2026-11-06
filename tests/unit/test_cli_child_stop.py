"""A long child of a `loom code` subcommand (the scip-full harness, the bare
clone) stops with the command: a cancelled wait stops the child's whole
process group, and SIGTERM to the command unwinds it (children stopped,
`finally` blocks run) and exits 143 instead of dying on the spot."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from server.cli._proc import TERMINATED_EXIT, wait_or_stop

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

ROOT = Path(__file__).resolve().parents[2]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(stat.stdout.strip()) and not stat.stdout.strip().startswith("Z")


def _read_pid(path: Path) -> int:
    text = path.read_text().strip() if path.exists() else ""
    return int(text) if text else 0


async def _wait_for_file(path: Path) -> int:
    async with asyncio.timeout(10):
        while not (pid := _read_pid(path)):  # noqa: ASYNC110 — another process writes it
            await asyncio.sleep(0.02)
    return pid


async def _gone(*pids: int) -> None:
    async with asyncio.timeout(5):
        while any(_alive(p) for p in pids):  # noqa: ASYNC110 — polls another process
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("trap", ["", 'trap "" TERM; '])
async def test_a_cancelled_wait_stops_the_child_and_its_children(trap: str, tmp_path: Path) -> None:
    """A child that exits on SIGTERM is reaped at once; one that ignores it is
    SIGKILLed after the grace period. Either way no grandchild is left."""
    pidfile = tmp_path / "grandchild.pid"
    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        f'{trap}sleep 60 & echo $! > "{pidfile}"; wait',
        start_new_session=True,
    )
    task = asyncio.create_task(wait_or_stop(proc, grace_s=0.3))
    # The wait must be running when it is cancelled: a task cancelled before its
    # first step never enters `wait_or_stop`, and the pid file can already exist.
    await asyncio.sleep(0)
    grandchild = await _wait_for_file(pidfile)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.returncode is not None  # reaped before the cancellation surfaced
        await _gone(grandchild)
    finally:
        for pid in (proc.pid, grandchild):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


async def test_a_finished_child_returns_its_exit_code() -> None:
    proc = await asyncio.create_subprocess_exec("bash", "-c", "exit 3", start_new_session=True)
    assert await wait_or_stop(proc) == 3


def test_sigterm_unwinds_a_code_subcommand_and_exits_143(tmp_path: Path) -> None:
    """A wrapper's `timeout` and an operator's `kill` send SIGTERM: the
    command's child group is stopped, its `finally` runs, and the exit code
    says why."""
    pidfile = tmp_path / "grandchild.pid"
    marker = tmp_path / "finally.ran"
    script = textwrap.dedent(
        f"""
        import asyncio
        from unittest import mock

        from server.cli import code_run
        from server.cli._proc import wait_or_stop

        async def _sub() -> int:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", 'sleep 60 & echo $! > "{pidfile}"; wait',
                start_new_session=True,
            )
            try:
                return await wait_or_stop(proc)
            finally:
                open("{marker}", "w").write("1")

        async def _no_disconnect(coro):
            return await coro

        with mock.patch.object(code_run, "_then_disconnect", _no_disconnect):
            code_run._run("t", _sub())
        """
    )
    env = {**os.environ, "OTEL_SDK_DISABLED": "true"}
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, env=env)
    grandchild = 0
    try:
        deadline = time.monotonic() + 20
        while not (pidfile.exists() and pidfile.read_text().strip()):
            assert proc.poll() is None, "the command exited before its child started"
            assert time.monotonic() < deadline
            time.sleep(0.05)
        grandchild = int(pidfile.read_text())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == TERMINATED_EXIT
        assert marker.exists()
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild)
    finally:
        if proc.poll() is None:
            proc.kill()
        if grandchild and _alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)
