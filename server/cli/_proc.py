"""A CLI command stopped by SIGTERM unwinds like one interrupted by Ctrl-C.

A `loom code` subcommand that runs a harness for tens of minutes must not leave
it running when the command itself is terminated (a wrapper's `timeout`, an
operator's `kill`): the orphan keeps writing into the checkout the next run
moves, and holds its CPU. `cancel_on_sigterm` turns SIGTERM into a cancellation,
so the command's `finally` blocks stop its children. `run_interruptible` runs a
command on a loop that a signal wakes, so Ctrl-C never waits for the next event.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from types import FrameType
from typing import Any

TERMINATED_EXIT = 128 + signal.SIGTERM
"""The exit code of a command ended by SIGTERM (143), as a shell reports it."""


class Terminated(Exception):
    """The command was cancelled because the process received SIGTERM."""


_request_stop: ContextVar[Callable[[asyncio.Task[object]], bool] | None] = ContextVar(
    "cli_request_stop", default=None
)


def cancel_for_shutdown(task: asyncio.Task[object]) -> bool:
    """Cancel a task for shutdown, once for a command sharing the signal handler."""
    request = _request_stop.get()
    return task.cancel() if request is None else request(task)


async def cancel_on_sigterm[T](aw: Awaitable[T]) -> T:
    """Await `aw` with SIGTERM turned into a cancellation of the running task,
    so its `finally` blocks run (children stopped, temp files removed, the
    ClickHouse client closed) before the process exits; the cancellation
    surfaces as `Terminated`. Without a handler SIGTERM ends Python at once.
    Where no handler can be installed (not the main thread) `aw` runs as is."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    got_term = False
    stopping = False

    def _stop(target: asyncio.Task[object]) -> bool:
        nonlocal stopping
        if target is not task:
            return target.cancel()
        # A second shutdown request would interrupt cleanup in progress.
        if stopping:
            return False
        stopping = task.cancel()
        return stopping

    def _on_term() -> None:
        nonlocal got_term
        if task is not None and _stop(task):
            got_term = True

    previous_int = signal.getsignal(signal.SIGINT)

    def _on_int(signum: int, frame: FrameType | None) -> None:
        nonlocal stopping
        if task is not None and callable(previous_int):
            # asyncio.run handles Ctrl-C itself, including a second interrupt.
            pending = task.cancelling()
            previous_int(signum, frame)
            stopping = stopping or task.cancelling() > pending

    installed = False
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGTERM, _on_term)
        installed = True
    token = _request_stop.set(_stop)
    try:
        if installed and callable(previous_int):
            signal.signal(signal.SIGINT, _on_int)
        # Ctrl-C during handler setup reaches the runner before our wrapper does.
        if task is not None and task.cancelling():
            stopping = True
        return await aw
    except asyncio.CancelledError:
        if got_term:
            raise Terminated("terminated by SIGTERM") from None
        raise
    finally:
        _request_stop.reset(token)
        if installed:
            if signal.getsignal(signal.SIGINT) is _on_int and callable(previous_int):
                signal.signal(signal.SIGINT, previous_int)
            loop.remove_signal_handler(signal.SIGTERM)


def run_interruptible[T](main: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(main)` on a loop that every signal wakes.

    `asyncio.run` turns Ctrl-C into a cancellation with a `signal.signal` handler,
    which Python runs between two bytecodes of the main thread. A signal that
    arrives as the loop enters its selector, or on another thread, only sets a
    flag: the handler then waits for the selector's next event, which can be a
    timer minutes away. A wakeup descriptor the loop reads makes the signal that
    event. Handlers and what they do stay as they were; where no descriptor can be
    set (not the main thread, a loop that watches none) `main` runs as is."""

    async def woken() -> T:
        loop = asyncio.get_running_loop()
        reader, writer = socket.socketpair()
        with reader, writer, contextlib.ExitStack() as undo:
            reader.setblocking(False)
            writer.setblocking(False)

            def drain() -> None:
                with contextlib.suppress(BlockingIOError):
                    while reader.recv(4096):
                        pass

            def restore(previous: int) -> None:
                # `loop.add_signal_handler` in `main` sets the loop's own: that one stays.
                current = signal.set_wakeup_fd(previous)
                if current != writer.fileno():
                    signal.set_wakeup_fd(current)

            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_reader(reader, drain)
                undo.callback(loop.remove_reader, reader)
                previous = signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
                undo.callback(restore, previous)
            return await main

    return asyncio.run(woken())
