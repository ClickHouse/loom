"""Fallback abort cannot replace the owner's selected first error."""

import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("owner", ["native", "supervise"])
@pytest.mark.parametrize(
    "first_kind", ["ValueError", "CancelledError", "SystemExit", "KeyboardInterrupt"]
)
@pytest.mark.parametrize(
    "abort_kind", ["none", "RuntimeError", "CancelledError", "SystemExit", "KeyboardInterrupt"]
)
def test_actual_attach_owner_preserves_first_error_through_abort(owner, first_kind, abort_kind):
    program = textwrap.dedent(
        """
        import asyncio
        import sys
        import time
        from server.cli import cell_attach_pg as apg
        from server.cli import cell_control
        from server.core import pg

        owner, first_kind, abort_kind = sys.argv[1:]
        kinds = dict(ValueError=ValueError, RuntimeError=RuntimeError,
                     CancelledError=asyncio.CancelledError, SystemExit=SystemExit,
                     KeyboardInterrupt=KeyboardInterrupt)
        first = kinds[first_kind]('first')
        events = []
        deadlines = []
        class Conn:
            def is_closed(self): return False
            async def close(self, **kwargs): events.append('public close')
            def terminate(self):
                events.append('abort')
                if abort_kind != 'none': raise kinds[abort_kind]('secondary abort')

        async def main():
            reaping, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            children = []
            async def work():
                children.append(asyncio.current_task())
                entered.set()
                try:
                    if owner == 'native' or first_kind != 'CancelledError': raise first
                    await asyncio.Future()
                finally:
                    if owner == 'supervise' and first_kind == 'CancelledError':
                        reaping.set()
                        await release.wait()
            async def close(conn, *, end):
                deadlines.append(end)
                events.append('public close')
                if owner == 'native':
                    children.append(asyncio.current_task())
                    raise RuntimeError('secondary close')
            original_close = pg.close_owned
            pg.close_owned = close
            original_wait = asyncio.wait
            waits = []
            async def wait(tasks, *, timeout):
                waits.append(timeout)
                if len(waits) == 2 and first_kind != 'CancelledError':
                    # Inject a second signal specifically during finalization.
                    reaping.set()
                    await release.wait()
                return await original_wait(tasks, timeout=timeout)
            if owner == 'supervise': asyncio.wait = wait
            end = time.monotonic() + 1
            owned = cell_control.Owner()
            owned.conn = Conn()
            async def operation():
                try:
                    if owner == 'native':
                        result = await cell_control._captured(
                            lambda: owned.call(work, total_s=1)
                        )
                        if isinstance(result, cell_control._Fatal): raise result.error
                    else:
                        await apg.supervise(Conn(), work(), end=end)
                except BaseException as error:
                    if first_kind == 'CancelledError' and owner == 'supervise':
                        assert isinstance(error, asyncio.CancelledError) and error.args == ('first',)
                    else:
                        assert error is first, (type(error).__name__, events)
                    return
                raise AssertionError('first error was swallowed')
            try:
                task = asyncio.create_task(apg._work_result(operation))
                if owner == 'supervise':
                    await entered.wait()
                    if first_kind == 'CancelledError': task.cancel('first')
                    await reaping.wait()
                    task.cancel('second')
                result = await task
                if isinstance(result, apg._Interrupted): raise result.error
                assert events.count('abort') == 1, events
                assert events.index('public close') < events.index('abort'), events
                expected_end = owned.phase_end if owner == 'native' else end
                assert deadlines == [expected_end], deadlines
                if owner == 'native': assert owned.close_end == owned.phase_end
                assert all(0 <= bound <= 1 for bound in waits), waits
            finally:
                pg.close_owned = original_close
                asyncio.wait = original_wait
                release.set()
                await asyncio.gather(*children, return_exceptions=True)
                assert all(child.done() and not child._log_traceback for child in children)
            print('first preserved and children reaped')
        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program, owner, first_kind, abort_kind],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "first preserved and children reaped" in result.stdout
