"""The shared attach owner finishes before errors reach the command boundary."""

import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("owner", ["lock", "mode"])
@pytest.mark.parametrize("secondary", ["RuntimeError", "SystemExit", "KeyboardInterrupt"])
def test_actual_attach_owner_keeps_first_failure(owner, secondary):
    program = textwrap.dedent(
        """
        import argparse
        import asyncio
        import sys
        from unittest.mock import AsyncMock
        from server.cli.cell_attach import Run
        from server.cli import cell_attach_pg as apg
        from server.cli import cell_control
        from tests.unit.test_cell_control import snapshot

        owner, kind = sys.argv[1:]
        original = ValueError('operation failed')
        secondary = {'RuntimeError': RuntimeError, 'SystemExit': SystemExit,
                     'KeyboardInterrupt': KeyboardInterrupt}[kind]('exit failed')
        events = []
        class Conn:
            def __init__(self):
                self.closed = False
                self.closes = 0
            def is_closed(self):
                return self.closed
            async def close(self, **kwargs):
                self.closes += 1
                events.append('close')
                self.closed = True
                raise secondary
            def terminate(self):
                self.closed = True
                events.append('abort')

        async def main():
            conn = Conn()
            async def opening(owned, what): owned.conn = conn
            async def fail(*args): raise original
            cell_control.Owner._open = opening
            router = AsyncMock()
            router.call.return_value = {**snapshot(), 'state': 'attaching'}
            run = Run(None, argparse.Namespace(cell='test', wait=0), router, lambda _: None)
            run.row = {'deployment_id': 'test'}
            run.mode = 'recording'
            run.confirm_lock = AsyncMock()
            run.ready_for_commit = AsyncMock(return_value=[])
            run.mutate = AsyncMock()
            run.read_own_row = AsyncMock(side_effect=original)
            apg._mode_transaction = fail
            async def mode(owned):
                run.owner, run.lock_conn = owned, owned.conn
                return await run.commit_mode()
            try:
                await (run.run() if owner == 'lock' else cell_control.run('attach', mode))
            except BaseException as error:
                assert error is original, (type(error).__name__, events)
                assert events.count('close') == 1, events
                await asyncio.sleep(0)
                print('original-after-owned-exit')
                return
            raise AssertionError('operation failure was swallowed')
        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program, owner, secondary],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "original-after-owned-exit" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("factory_failure", [False, True])
async def test_mode_failure_survives_close_construction_and_await_failures(
    monkeypatch, factory_failure
):
    import argparse
    from unittest.mock import AsyncMock

    from server.cli import cell_attach_pg as apg
    from server.cli import cell_control
    from server.cli.cell_attach import Run
    from server.core import pg

    original = ValueError("mode failed")
    observed = []
    conn = AsyncMock()
    conn.is_closed = lambda: False
    conn.terminate = lambda: None
    owned = cell_control.Owner()

    async def opening():
        owned.conn = conn

    async def failed_close():
        raise RuntimeError("close failed")

    def close(actual, *, end):
        observed.append((actual, end))
        if factory_failure:
            raise RuntimeError("close construction failed")
        return failed_close()

    monkeypatch.setattr(pg, "close_owned", close)
    commit = AsyncMock(side_effect=original)
    monkeypatch.setattr(apg, "commit_enforced", commit)
    run = Run(None, argparse.Namespace(cell="test", wait=0), AsyncMock(), lambda _: None)
    run.owner, run.lock_conn = owned, conn
    run.row = {"deployment_id": "test"}
    run.mode = "recording"
    run.mutate = AsyncMock()
    with pytest.raises(ValueError) as result:
        await owned.run(opening, run.commit_mode, opening_s=0.3)
    assert result.value is original and original.__traceback__ is not None
    commit.assert_awaited_once()
    assert observed == [(conn, owned.close_end)]
    assert owned.closed and owned._close_task.done()
    assert not owned._close_task._log_traceback


@pytest.mark.asyncio
async def test_successful_mode_work_still_fails_when_required_close_fails(monkeypatch):
    import argparse
    from unittest.mock import AsyncMock

    from server.cli import cell_attach_pg as apg
    from server.cli import cell_control
    from server.cli.cell_attach import Run

    original = OSError("public close failed")
    conn = AsyncMock()
    conn.is_closed = lambda: False
    conn.terminate = lambda: None
    conn.close.side_effect = original
    owned = cell_control.Owner()

    async def opening():
        owned.conn = conn

    async def committed(*args, **kwargs):
        args[-1]("unknown")

    monkeypatch.setattr(apg, "commit_enforced", committed)
    run = Run(None, argparse.Namespace(cell="test", wait=0), AsyncMock(), lambda _: None)
    run.owner, run.lock_conn = owned, conn
    run.row = {"deployment_id": "test"}
    run.mutate = AsyncMock()
    with pytest.raises(OSError) as result:
        await owned.run(opening, run.commit_mode, opening_s=0.3)
    assert result.value is original
    assert run.mode == "unknown"
    conn.close.assert_awaited_once()
