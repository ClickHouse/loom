"""Child processes of a CLI command: a long child is stopped with the command.

A `loom code` subcommand that runs a harness or a clone for tens of minutes
must not leave it running when the command itself is interrupted (Ctrl-C) or
terminated (the cron's `timeout`, an operator's `kill`): the orphan keeps
writing into the checkout the next run moves, and holds its CPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable

STOP_GRACE_S = 10.0
"""How long a child's process group has to exit after SIGTERM before SIGKILL."""

TERMINATED_EXIT = 128 + signal.SIGTERM
"""The exit code of a command ended by SIGTERM (143), as a shell reports it."""


class Terminated(Exception):
    """The command was cancelled because the process received SIGTERM."""


async def wait_or_stop(proc: asyncio.subprocess.Process, *, grace_s: float = STOP_GRACE_S) -> int:
    """`proc.wait()`; when the wait is abandoned (the command cancelled), the
    child's process group gets SIGTERM, then SIGKILL after `grace_s`, and the
    child is reaped before the cancellation propagates.

    `proc` must have been started with `start_new_session=True`: its pid is
    then its process group id, and the group holds the grandchildren a
    signal to the child alone would orphan (a harness's compiler, git's
    remote helper)."""
    try:
        return await proc.wait()
    except BaseException:
        await _stop_group(proc, grace_s)
        raise


async def _stop_group(proc: asyncio.subprocess.Process, grace_s: float) -> None:
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
    except TimeoutError:
        pass
    # The group is killed even when the child already exited: a grandchild
    # that ignored SIGTERM is what would be left running.
    _signal_group(proc.pid, signal.SIGKILL)
    await proc.wait()


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def cancel_on_sigterm[T](aw: Awaitable[T]) -> T:
    """Await `aw` with SIGTERM turned into a cancellation of the running task,
    so its `finally` blocks run (children stopped, temp files removed, the
    ClickHouse client closed) before the process exits; the cancellation
    surfaces as `Terminated`. Without a handler SIGTERM ends Python at once.
    Where no handler can be installed (not the main thread) `aw` runs as is."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    got_term = False

    def _on_term() -> None:
        nonlocal got_term
        got_term = True
        if task is not None:
            task.cancel()

    installed = False
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGTERM, _on_term)
        installed = True
    try:
        return await aw
    except asyncio.CancelledError:
        if got_term:
            raise Terminated("terminated by SIGTERM") from None
        raise
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)
