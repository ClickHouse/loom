"""A CLI command stopped by SIGTERM unwinds like one interrupted by Ctrl-C.

A `loom code` subcommand that runs a harness for tens of minutes must not leave
it running when the command itself is terminated (a wrapper's `timeout`, an
operator's `kill`): the orphan keeps writing into the checkout the next run
moves, and holds its CPU. `cancel_on_sigterm` turns SIGTERM into a cancellation,
so the command's `finally` blocks stop its children.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable

TERMINATED_EXIT = 128 + signal.SIGTERM
"""The exit code of a command ended by SIGTERM (143), as a shell reports it."""


class Terminated(Exception):
    """The command was cancelled because the process received SIGTERM."""


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
