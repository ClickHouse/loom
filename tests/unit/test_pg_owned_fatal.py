"""Owned child tasks preserve native failures until their supervisor cleans up."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("kind", ["SystemExit", "KeyboardInterrupt"])
@pytest.mark.parametrize(
    "phase",
    ["close", "migration", "migration_exit", "migration_ordinary_exit", "migration_exit_stall"],
)
def test_native_failure_reaches_the_owner_after_cleanup(kind: str, phase: str, tmp_path) -> None:
    program = textwrap.dedent(
        """
        import asyncio
        import contextlib
        import sys
        import time
        from pathlib import Path
        from server.core import pg
        from server.core.schema import pg_migrate

        kind, phase, path = sys.argv[1:]
        fatal_type = SystemExit if kind == 'SystemExit' else KeyboardInterrupt
        original = ValueError('original') if 'ordinary' in phase else fatal_type('original')
        secondary = fatal_type('cleanup')
        events = []

        class Connection:
            async def close(self, **kwargs):
                events.append('close')
                if phase == 'close':
                    raise original
                if phase in {'migration_exit', 'migration_ordinary_exit'}:
                    raise secondary
            def terminate(self):
                events.append('abort')

        conn = Connection()

        @contextlib.asynccontextmanager
        async def connect():
            try:
                yield conn
            finally:
                events.append('exit')
                if phase == 'migration_exit_stall':
                    await asyncio.Future()
                if phase in {'migration_exit', 'migration_ordinary_exit'}:
                    raise secondary

        async def fail(*args):
            events.append('work')
            raise original

        async def main():
            pg_migrate._migrate = fail
            pg._RELEASE_TIMEOUT_S = 0.05
            started = time.monotonic()
            try:
                if phase == 'close':
                    await pg.close_owned(conn, end=time.monotonic() + 1)
                else:
                    await pg_migrate.migrate(Path(path), connect=connect, schema='test')
            except BaseException as error:
                assert error is original, (type(error).__name__, events)
                assert time.monotonic() - started < 0.3, events
                assert 'close' in events, events
                if phase != 'close':
                    assert 'exit' in events, events
                    assert events.index('close') < events.index('exit'), events
                else:
                    assert 'abort' in events, events
                await asyncio.sleep(0)
                print('owner-received-original-after-cleanup')
                return
            raise AssertionError('native failure was swallowed')

        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program, kind, phase, str(tmp_path)],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "owner-received-original-after-cleanup" in result.stdout


@pytest.mark.parametrize("connection_context", ["cell", "router"])
@pytest.mark.parametrize("kind", ["SystemExit", "KeyboardInterrupt", "ValueError"])
@pytest.mark.parametrize("exit_behavior", ["fatal", "stalled"])
def test_actual_connection_context_keeps_first_failure_and_cleanup_end(
    connection_context: str, kind: str, exit_behavior: str, tmp_path
) -> None:
    program = textwrap.dedent(
        """
        import asyncio
        import sys
        import time
        from pathlib import Path
        import asyncpg
        from server.core import pg
        from server.core.schema import pg_migrate
        from server.router import directory

        context, kind, exit_behavior, path = sys.argv[1:]
        original = {'SystemExit': SystemExit, 'KeyboardInterrupt': KeyboardInterrupt,
                    'ValueError': ValueError}[kind]('original')
        secondary = SystemExit('context failed')
        events = []

        class Connection:
            async def close(self, **kwargs):
                events.append('close')
                if events.count('close') > 1:
                    if exit_behavior == 'stalled':
                        await asyncio.Future()
                    raise secondary
            def terminate(self):
                events.append('abort')

        conn = Connection()
        async def raw(*args, **kwargs):
            return conn
        async def checked(*args):
            pass
        async def fail(*args):
            raise original

        async def main():
            pg.settings.pg_dsn = "postgresql://unused/test"
            pg._check_deployment = checked
            pg.init_connection = checked
            asyncpg.connect = raw
            directory._require_dsn = lambda: 'unused-test-dsn'
            pg._RELEASE_TIMEOUT_S = 0.05
            pg_migrate._migrate = fail
            connect = None if context == 'cell' else directory._dedicated
            started = time.monotonic()
            try:
                await pg_migrate.migrate(Path(path), connect=connect, schema='test')
            except BaseException as error:
                assert error is original, (type(error).__name__, events)
                assert time.monotonic() - started < 0.3, events
                assert events.count('close') == 2, events
                if exit_behavior == 'stalled':
                    assert 'abort' in events, events
                await asyncio.sleep(0)
                print('first-failure-and-original-end-preserved')
                return
            raise AssertionError('first migration failure was swallowed')
        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program, connection_context, kind, exit_behavior, str(tmp_path)],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "first-failure-and-original-end-preserved" in result.stdout


@pytest.mark.parametrize("exit_error", ["RuntimeError", "SystemExit", "KeyboardInterrupt"])
def test_synchronous_context_exit_failure_keeps_original_migration_error(
    exit_error: str, tmp_path
) -> None:
    program = textwrap.dedent(
        """
        import asyncio
        import sys
        from pathlib import Path
        from server.core.schema import pg_migrate

        kind, path = sys.argv[1:]
        original = ValueError('original migration error')
        secondary = {'RuntimeError': RuntimeError, 'SystemExit': SystemExit,
                     'KeyboardInterrupt': KeyboardInterrupt}[kind]('context factory failed')
        events = []
        class Connection:
            async def close(self, **kwargs):
                events.append('close')
            def terminate(self):
                events.append('abort')
        class Context:
            async def __aenter__(self):
                return Connection()
            def __aexit__(self, kind, value, traceback):
                assert value is original
                events.append('exit')
                raise secondary
        async def fail(*args):
            raise original
        async def main():
            pg_migrate._migrate = fail
            try:
                await pg_migrate.migrate(Path(path), connect=Context, schema='test')
            except BaseException as error:
                assert error is original, (type(error).__name__, events)
                assert events[:2] == ['close', 'exit'], events
                await asyncio.sleep(0)
                print('synchronous-context-failure-contained')
                return
            raise AssertionError('migration failure was swallowed')
        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program, exit_error, str(tmp_path)],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synchronous-context-failure-contained" in result.stdout
