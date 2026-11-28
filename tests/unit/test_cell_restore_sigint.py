"""The public CLI names an unknown outcome after the real Runner SIGINT boundary."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import textwrap

import pytest


async def test_sigint_prints_retry_diagnostic_before_exiting() -> None:
    script = textwrap.dedent("""
        import argparse
        import asyncio
        import httpx
        from server.cli import cell_restore
        from server.core import ch

        async def no_io(*args, **kwargs):
            pass

        async def work(*args, **kwargs):
            print('RESTORE_READY', flush=True)
            await asyncio.Future()

        cell_restore._args_problem = lambda args: ''
        cell_restore.cell_cli.operator_router = lambda args: httpx.URL('https://router.test')
        cell_restore._connect = no_io
        cell_restore.disconnect = no_io
        cell_restore.Restore.run = work
        ch.get_client = lambda: object()
        cell_restore.cmd_restore(argparse.Namespace(cell='first', wait=10))
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env={**os.environ, "LOOM_ENV_FILE": ""},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"RESTORE_READY\n"
        process.send_signal(signal.SIGINT)
        _, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode != 0
        assert b"outcome may be unknown" in stderr
        assert b"rerun the same command before starting the cell" in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_native_child_fatal_does_not_stop_restore_loop_before_owner_close() -> None:
    script = textwrap.dedent("""
        import asyncio
        from server.cli import cell_restore, cell_control

        fatal = KeyboardInterrupt('native child failure')
        class Connection:
            async def close(self, **kwargs):
                await asyncio.sleep(0.01)
                print('PUBLIC_CLOSE_DONE', flush=True)
            def terminate(self):
                print('ABORT', flush=True)

        async def failure():
            raise fatal

        async def main():
            budget = cell_restore.Budget(1)
            budget.conn = Connection()
            try:
                await budget.call(failure())
            except cell_control._ForwardFatal as transported:
                assert transported.fatal.error is fatal
                print('LOOP_STILL_RUNNING', flush=True)
                await asyncio.sleep(0)
                raise transported.fatal.error

        try:
            asyncio.run(main())
        except KeyboardInterrupt as caught:
            assert caught is fatal
            print('IDENTICAL_FATAL_AT_BOUNDARY', flush=True)
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env={**os.environ, "LOOM_ENV_FILE": ""},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, stderr.decode()
        assert stdout.splitlines() == [
            b"PUBLIC_CLOSE_DONE",
            b"LOOP_STILL_RUNNING",
            b"IDENTICAL_FATAL_AT_BOUNDARY",
        ]
        assert b"never retrieved" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_native_public_close_fatal_returns_to_supervising_task() -> None:
    script = textwrap.dedent("""
        import asyncio
        from server.cli import cell_control
        from server.core import pg
        import time

        fatal = SystemExit('native close failure')
        class Connection:
            async def close(self, **kwargs):
                raise fatal
            def terminate(self):
                print('ABORT', flush=True)

        async def main():
            result = await asyncio.create_task(cell_control._captured(
                lambda: pg.close_owned(Connection(), end=time.monotonic() + 1)
            ))
            assert isinstance(result, cell_control._Fatal)
            assert result.error is fatal
            print('LOOP_STILL_RUNNING', flush=True)
            raise result.error

        try:
            asyncio.run(main())
        except SystemExit as caught:
            assert caught is fatal
            print('IDENTICAL_FATAL_AT_BOUNDARY', flush=True)
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env={**os.environ, "LOOM_ENV_FILE": ""},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, stderr.decode()
        assert stdout.splitlines() == [
            b"ABORT",
            b"LOOP_STILL_RUNNING",
            b"IDENTICAL_FATAL_AT_BOUNDARY",
        ]
        assert b"never retrieved" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("phase", ["initialization", "work", "close"])
@pytest.mark.parametrize("kind", ["SystemExit", "KeyboardInterrupt"])
async def test_public_restore_transports_identical_native_fatal_after_cleanup(phase, kind):
    script = textwrap.dedent(f"""
        import argparse
        import asyncio
        import asyncpg
        import httpx
        from server.cli import cell_restore
        from server.core import ch, pg
        from server.settings import settings

        settings.pg_dsn = "postgres://not-connected"

        phase = {phase!r}
        fatal = {kind}('native restore child failure')
        class Connection:
            async def close(self, **kwargs):
                if phase == 'close':
                    raise fatal
                await asyncio.sleep(0.01)
                print('PUBLIC_CLOSE_DONE', flush=True)
            def terminate(self):
                print('ABORT', flush=True)

        conn = Connection()
        async def no_io(*args, **kwargs):
            pass
        async def open_connection(*args, **kwargs):
            return conn
        async def initialize(conn):
            if phase == 'initialization':
                raise fatal
        async def direct(*args):
            raise fatal
        async def disconnect(*args):
            print('DISCONNECTED', flush=True)
        async def close_work(runner, ch):
            runner.budget.conn = conn
            await runner.budget.close()

        cell_restore._args_problem = lambda args: ''
        cell_restore.cell_cli.operator_router = lambda args: httpx.URL('https://router.test')
        cell_restore._connect = no_io
        cell_restore.disconnect = disconnect
        asyncpg.connect = open_connection
        pg._check_deployment = no_io
        pg.init_connection = initialize
        cell_restore.apg.direct_refusals = direct
        if phase == 'close':
            cell_restore.Restore.run = close_work
        ch.get_client = lambda: object()
        try:
            cell_restore.cmd_restore(argparse.Namespace(cell='first', wait=1))
        except {kind} as caught:
            assert caught is fatal
            print('IDENTICAL_FATAL_AT_BOUNDARY', flush=True)
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env={**os.environ, "LOOM_ENV_FILE": ""},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, stderr.decode()
        assert stdout.splitlines() == [
            b"ABORT" if phase == "close" else b"PUBLIC_CLOSE_DONE",
            b"DISCONNECTED",
            b"IDENTICAL_FATAL_AT_BOUNDARY",
        ]
        assert b"never retrieved" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
