"""SIGTERM to a `loom code` subcommand unwinds it: its `finally` blocks run and it
exits 143 instead of dying on the spot."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from server.cli._proc import TERMINATED_EXIT

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

ROOT = Path(__file__).resolve().parents[2]


def test_sigterm_unwinds_a_code_subcommand_and_exits_143(tmp_path: Path) -> None:
    """A wrapper's `timeout` and an operator's `kill` send SIGTERM: the
    command's `finally` runs, and the exit code says why."""
    started = tmp_path / "started"
    marker = tmp_path / "finally.ran"
    script = textwrap.dedent(
        f"""
        import asyncio
        from unittest import mock

        from server.cli import code_run

        async def _sub() -> int:
            open("{started}", "w").write("1")
            try:
                await asyncio.sleep(60)
                return 0
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
    try:
        deadline = time.monotonic() + 20
        while not started.exists():
            assert proc.poll() is None, "the command exited before it started"
            assert time.monotonic() < deadline
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == TERMINATED_EXIT
        assert marker.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
