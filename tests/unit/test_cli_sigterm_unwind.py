"""SIGTERM to a `loom code` subcommand unwinds it: its `finally` blocks run and it
exits 143 instead of dying on the spot."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from server.cli._proc import TERMINATED_EXIT, Terminated, cancel_on_sigterm

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


def test_sigterm_preserves_cleanup_started_by_sigint(tmp_path: Path) -> None:
    marker = tmp_path / "finally.ran"
    script = textwrap.dedent(
        f"""
        import asyncio
        import os
        import signal
        from pathlib import Path
        from unittest.mock import patch

        from server.cli._proc import TERMINATED_EXIT, Terminated, cancel_on_sigterm

        async def run():
            loop = asyncio.get_running_loop()
            with patch.object(loop, "add_signal_handler") as install:
                async def command():
                    try:
                        os.kill(os.getpid(), signal.SIGINT)
                        await asyncio.Future()
                    finally:
                        install.call_args.args[1]()
                        await asyncio.sleep(0)
                        Path({str(marker)!r}).touch()

                await cancel_on_sigterm(command())

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            raise SystemExit(130)
        except Terminated:
            raise SystemExit(TERMINATED_EXIT)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, timeout=20, check=False)
    assert result.returncode == 130
    assert marker.exists()


async def test_sigterm_stops_a_command_during_timeout_cleanup() -> None:
    loop = asyncio.get_running_loop()
    recovered = False

    async def command() -> None:
        nonlocal recovered
        try:
            async with asyncio.timeout(0):
                try:
                    await asyncio.Future()
                finally:
                    install.call_args.args[1]()
                    await asyncio.sleep(0)
        except TimeoutError:
            recovered = True

    with (
        patch.object(loop, "add_signal_handler") as install,
        patch.object(loop, "remove_signal_handler"),
        pytest.raises(Terminated, match="terminated by SIGTERM"),
    ):
        await cancel_on_sigterm(command())
    assert not recovered
