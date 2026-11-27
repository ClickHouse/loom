"""`loom code backfill --workers N`: the index phase's cursor batches and its edge pass
in N processes.

One process parses and extracts at about one core: every tree-sitter parse holds the
interpreter lock and runs one at a time per process (`bounded_parse`), and the chunk,
symbol and edge extraction after it walks the tree in Python. A parse tree does not
cross a process boundary, and a file's extraction reads the index between its steps
(name candidates, then the targets they resolve), so the unit a worker takes is a whole
cursor batch: its own connection, reads, parse, extraction and writes, as one process
runs them.

A worker is the command itself. It is started with the command's own parsed arguments
and enters through `dispatch.run_parsed`, as `loom` does, so it runs under every binding
the command runs under (traffic class, query tag, operation identity, the org it bills,
logging, telemetry, signal handling, the model-work admission and the registry read);
`_backfill` then serves the batches it is sent instead of walking the tree. One pipe
per worker carries its batches and their counters. The parent hands a worker its next
batch when the worker reports its last, so nothing runs that it did not hand out.

A worker leaves when its pipe closes: between batches it returns and retires as the
command does; during a batch, the parent stopped or died, so the closing pipe cancels
the command's task as a SIGTERM or a Ctrl-C would, and the batch unwinds before the
worker retires and exits non-zero. The parent reads every worker's exit code.

Each worker caches what it reads of the index (header texts, the live path set, the
resolver's layouts and listings), and another worker's writes do not reach those caches.
A worker forgets them whenever it moves to the next group of batches: the sources once
the headers are written, then the edge pass, which therefore reads the finished index.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import multiprocessing
import signal
import sys
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any

from server.cli.dispatch import run_parsed
from server.code.app.index.ingest_headers import HEADER_EXTS

_SPAWN = multiprocessing.get_context("spawn")  # a fresh interpreter: no copy of the parent's loop

# (first index, paths, counters, why the org's switch held it: "" once written)
Done = tuple[int, list[str], dict[str, int], str]


@dataclass(frozen=True)
class Assignment:
    """What a worker process serves: the parent's head, its number, its end of the pipe."""

    head: str
    number: int
    conn: Connection


@dataclass
class _Worker:
    number: int
    process: Any  # multiprocessing.context.SpawnProcess
    conn: Connection

    def send(self, kind: str, group: str, paths: list[str]) -> None:
        # A worker that exited between batches has closed its end: `receive` then
        # reads the end of the pipe, and its exit code says why.
        with contextlib.suppress(OSError):
            self.conn.send((kind, group, paths))

    async def receive(self) -> Any:
        """The worker's next message, None once it has exited."""
        await _readable(self.conn.fileno())
        try:
            return self.conn.recv()
        except (EOFError, OSError):
            return None

    async def exit_code(self) -> int:
        await _readable(self.process.sentinel)
        self.process.join()
        return int(self.process.exitcode)


class Workers:
    """The worker processes of one run, for both of its passes."""

    def __init__(self, workers: list[_Worker]) -> None:
        self._workers = workers

    async def index_batches(
        self, batches: list[tuple[int, list[str]]]
    ) -> AsyncGenerator[Done, None]:
        """Each batch with its counters and why it was held, in batch order. The header
        batches are all written before the first source batch is handed out. Once a batch
        is held nothing more is handed out; the batches still running finish and are
        yielded after the ones before them."""
        held = False
        for group, part in _groups(batches):
            if held:
                return
            order = deque(b for b, _paths in part)
            ready: dict[int, Done] = {}
            async with contextlib.aclosing(self._run("index", group, part)) as finished:
                async for done in finished:
                    ready[done[0]] = done
                    held = held or bool(done[3])
                    while order and order[0] in ready:
                        yield ready.pop(order.popleft())
            for b in sorted(ready):  # after a hold: those that ran beside it
                yield ready[b]

    async def edge_batches(self, batches: list[tuple[int, list[str]]]) -> list[dict[str, int]]:
        """The edge pass's batches, in any order; each one's counters."""
        async with contextlib.aclosing(self._run("edges", "edges", batches)) as finished:
            return [counters async for _b, _p, counters, _h in finished]

    async def _run(
        self, kind: str, group: str, batches: list[tuple[int, list[str]]]
    ) -> AsyncGenerator[Done, None]:
        """`batches` across the workers as each one finishes its last, yielded as they
        finish; none handed out once one comes back held."""
        todo = deque(batches)
        idle = list(self._workers)
        running: dict[asyncio.Future[Any], tuple[_Worker, int, list[str]]] = {}
        held = False
        try:
            while running or (todo and not held):
                while todo and idle and not held:
                    worker, (b, paths) = idle.pop(), todo.popleft()
                    worker.send(kind, group, paths)
                    running[asyncio.ensure_future(worker.receive())] = (worker, b, paths)
                finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for fut in finished:
                    worker, b, paths = running.pop(fut)
                    reply = fut.result()
                    if reply is None:
                        code = await worker.exit_code()
                        raise SystemExit(
                            f"backfill worker {worker.number} {_ended(code)} during the {kind} "
                            f"batch at {b} ({len(paths)} files)" + _above(code)
                        )
                    _tag, counters, why = reply
                    held = held or bool(why)
                    idle.append(worker)
                    yield b, paths, counters, why
        finally:
            for fut in running:
                fut.cancel()


def _groups(batches: list[tuple[int, list[str]]]) -> list[tuple[str, list[tuple[int, list[str]]]]]:
    """The header batches, then the source batches (`_cursor_batches` never mixes them)."""
    headers = [(b, p) for b, p in batches if p[0].endswith(HEADER_EXTS)]
    sources = [(b, p) for b, p in batches if not p[0].endswith(HEADER_EXTS)]
    return [(g, part) for g, part in (("headers", headers), ("sources", sources)) if part]


def side_by_side(batches: list[tuple[int, list[str]]]) -> bool:
    """Whether N workers would write any of `batches` beside another of its group."""
    return any(len(part) > 1 for _g, part in _groups(batches))


@contextlib.asynccontextmanager
async def opened(n: int, args: argparse.Namespace, head: str) -> AsyncIterator[Workers]:
    """N workers running `args` at `head`, each ready (its run opened) when this yields.
    On a normal exit their pipes close and each one retires; a worker that exits
    non-zero then (its drain or retirement failed) fails the run, so the caller keeps
    its own durable end inside the block. On an exception or a cancellation the pipes
    close too, and each worker leaves its batch at once."""
    workers = [_start(i, args, head) for i in range(1, n + 1)]
    try:
        for worker in workers:
            if await worker.receive() is None:
                code = await worker.exit_code()
                raise SystemExit(
                    f"backfill worker {worker.number} {_ended(code)} before its run opened"
                    + _above(code)
                )
        yield Workers(workers)
    except BaseException:  # stop every worker, then raise the run's own failure
        await _close(workers)
        raise
    codes = await _close(workers)
    failed = [(w.number, code) for w, code in zip(workers, codes, strict=True) if code]
    if failed:
        raise SystemExit(
            "backfill workers did not retire cleanly ("
            + ", ".join(f"worker {n} {_ended(code)}" for n, code in failed)
            + ")"
            + ("; their errors are above" if any(code > 0 for _n, code in failed) else "")
        )


def _ended(code: int) -> str:
    """How a worker process ended: its exit code, or the signal that killed it."""
    if code >= 0:
        return f"exited {code}"
    name = signal.Signals(-code).name
    return f"was killed by {name}" + (" (out of memory?)" if -code == signal.SIGKILL else "")


def _above(code: int) -> str:
    """Where the operator finds why: a worker that exited printed it; a killed one did not."""
    return "; its error is above" if code > 0 else ""


def _start(number: int, args: argparse.Namespace, head: str) -> _Worker:
    mine, theirs = _SPAWN.Pipe()
    command = argparse.Namespace(
        **vars(args), backfill_worker=Assignment(head=head, number=number, conn=theirs)
    )
    process = _SPAWN.Process(
        target=_worker_main, args=(command,), name=f"loom-backfill-worker-{number}"
    )
    process.start()
    theirs.close()  # the worker's end lives in the worker only: either side's exit is an EOF
    return _Worker(number=number, process=process, conn=mine)


async def _close(workers: list[_Worker]) -> list[int]:
    for worker in workers:
        worker.conn.close()
    return [await worker.exit_code() for worker in workers]


def _worker_main(command: argparse.Namespace) -> None:
    """A worker process: the command, entered as `loom` enters it."""
    try:
        code = run_parsed(command)
    except KeyboardInterrupt:  # the parent reports the interruption; one traceback is enough
        code = 130
    sys.exit(code)


async def serve(run: Any, assignment: Assignment) -> int:
    """A worker's share of the command: the batches the parent sends, each awaited in
    this command's own task, until the parent closes the pipe. A pipe that closes during
    a batch cancels that task, as a SIGTERM or a Ctrl-C does, so every stop unwinds
    through the batch before the command retires."""
    from server.cli.code_backfill import edge_counters, index_counters
    from server.cli.code_run import _note
    from server.code.app.index.ingest_resolve import forget_index_reads

    async def _batch(kind: str, paths: list[str]) -> tuple[dict[str, int], str]:
        if kind == "index":
            run.counters = index_counters()
            held = await run.batch(paths)
            return run.counters, held
        run.edges = edge_counters()
        await run.edge_batch(paths)
        return run.edges, ""

    conn = assignment.conn
    _note(worker=assignment.number)
    try:
        conn.send(("ready",))
    except OSError:
        return 0  # the run stopped while this worker opened (another worker was refused)
    group = ""
    while True:
        await _readable(conn.fileno())
        try:
            kind, batch_group, paths = conn.recv()
        except (EOFError, OSError):
            return 0  # the run is done
        if batch_group != group:
            # What this process read of the index may predate another worker's writes.
            forget_index_reads()
            group = batch_group
        counters, held = await _until_closed(conn, _batch(kind, paths), assignment.number)
        try:
            conn.send(("done", counters, held))
        except OSError:
            return 1  # the run stopped as this batch finished


async def _until_closed[T](conn: Connection, work: Coroutine[Any, Any, T], number: int) -> T:
    """`work`, awaited in this task, which a closing pipe cancels; that cancellation
    becomes the worker's exit, any other (a signal) stays a cancellation."""
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    closed = False

    def _on_readable() -> None:  # the parent sends nothing during a batch: it went away
        nonlocal closed
        # Once: a closed pipe stays readable, and the batch's unwind must not spin on it.
        loop.remove_reader(conn.fileno())
        if task.cancelling() == 0:  # a signal already unwinding the batch keeps its exit
            closed = True
            task.cancel()

    loop.add_reader(conn.fileno(), _on_readable)
    try:
        return await work
    except asyncio.CancelledError:
        if closed and task.uncancel() == 0:
            raise SystemExit(
                f"backfill worker {number}: the run stopped during its batch; the batch was left"
            ) from None
        raise
    finally:
        loop.remove_reader(conn.fileno())


async def _readable(fd: int) -> None:
    """Until `fd` can be read (data, or its other end closed)."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def _ready() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(fd, _ready)
    try:
        await ready
    finally:
        loop.remove_reader(fd)
