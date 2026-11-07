"""The lease holder's background workers, in a child process.

In `auto` mode the holder keeps the lease and starts this child to run the
supervisor, so a long pass never shares the event loop that serves requests:
no port, no serving lifespan, only what the workers use (`server/worker_main.py`,
the child's composition root).

The parent writes the lease's deadline (on the host's monotonic clock, which
both processes share) down a pipe every second; the deadline stops moving
once the lease is no longer fresh. The child's passes stop at that deadline,
as they would in the parent, and the child is gone by the deadline plus the
drain time (T/6): it exits by itself then, the parent kills it then, and the
kernel kills it when the parent dies. The pipe closing stops it too.

The child's event loop writes a beat down a second pipe every second; a child
whose beats stopped (a hung loop) is not `task_alive` in /v1/health.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import fcntl
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping

from server.core.errors import error_kind
from server.settings import settings
from server.workers import leader
from server.workers.supervisor import WorkerSupervisor, get_supervisor

log = logging.getLogger("loom.workers.child")

# How often the parent writes the deadline down the pipe, and the child its beat.
PUBLISH_PERIOD_S = 1.0
# A child with no beat for this long is not alive (also its start-up grace).
ALIVE_WINDOW_S = 15.0
# The longest a stop waits for the child before killing it, whatever the
# deadline: the lifespan's shutdown drains share 40 s.
STOP_TIMEOUT_S = 20.0
RESTART_DELAY_S = 5.0
# A child that exits sooner than this after it started failed to start; after
# MAX_FAILED_STARTS in a row this process gives the lease up.
MIN_UPTIME_S = 60.0
MAX_FAILED_STARTS = 5

_PR_SET_PDEATHSIG = 1


# --- parent side --------------------------------------------------------------


class ChildWorkers:
    """The supervisor's start/stop/status surface, backed by a child process."""

    def __init__(self, lease: leader.SupervisorLease) -> None:
        self._lease = lease
        self._proc: asyncio.subprocess.Process | None = None
        self._watch: asyncio.Task[None] | None = None
        self._publish: asyncio.Task[None] | None = None
        self._write_fd: int | None = None
        self._alive_fd: int | None = None
        self._beat_at = 0.0
        # The deadline last sent to the child.
        self._deadline = 0.0
        self.restarts = 0
        self.failed_starts = 0
        self.last_exit: int | None = None

    def start_all(self) -> None:
        if self._watch is None or self._watch.done():
            self._watch = asyncio.get_running_loop().create_task(
                self._keep_running(), name="loom-workers-child"
            )

    async def stop_all(self, drain_timeout_s: float = 10.0) -> None:
        """SIGTERM the child, and SIGKILL it at the lease deadline plus
        `drain_timeout_s`, or after STOP_TIMEOUT_S, whichever comes first. A
        cancelled stop kills it at once."""
        kill_at = min(self._deadline + drain_timeout_s, time.monotonic() + STOP_TIMEOUT_S)
        if self._watch is not None:
            self._watch.cancel()
            await asyncio.gather(self._watch, return_exceptions=True)
        await self._reap(kill_at)

    def status(self) -> list[dict[str, object]]:
        proc = self._proc
        return [
            {
                "name": "workers-child",
                "pid": proc.pid if proc is not None else None,
                "task_alive": self._alive(),
                "restarts": self.restarts,
                "failed_starts": self.failed_starts,
                "last_exit": self.last_exit,
            }
        ]

    def _alive(self) -> bool:
        """Running, and its loop beat within ALIVE_WINDOW_S."""
        proc = self._proc
        running = proc is not None and proc.returncode is None
        return running and time.monotonic() - self._beat_at < ALIVE_WINDOW_S

    async def _keep_running(self) -> None:
        """Start the child; start it again when it exits unasked while the
        lease is fresh. A child that keeps failing to start gives the lease up."""
        while True:
            started = time.monotonic()
            try:
                await self._spawn()
            except OSError as e:  # no child exists: fork, exec or a pipe failed
                self._close_pipe()
                self._proc = None
                # The outcome reaches `/v1/health` (no token) as the lease's reason.
                log.warning("workers child could not start: %s: %s", type(e).__name__, e)
                outcome = f"could not start ({error_kind(e)})"
            else:
                assert self._proc is not None
                self.last_exit = await self._proc.wait()
                self._close_pipe()
                outcome = f"exited (code {self.last_exit})"
            if not self._lease.fresh():
                return  # the lease loop stops the workers, and starts them once it is
            short = time.monotonic() - started < MIN_UPTIME_S
            self.failed_starts = self.failed_starts + 1 if short else 0
            if self.failed_starts >= MAX_FAILED_STARTS:
                why = (
                    f"the workers child failed {self.failed_starts} starts in a row "
                    f"(last: {outcome})"
                )
                log.error("%s; giving the lease up", why)
                await asyncio.to_thread(self._lease.give_up, why)
                return
            log.error("workers child %s; restarting in %.0f s", outcome, RESTART_DELAY_S)
            self.restarts += 1
            await asyncio.sleep(RESTART_DELAY_S)

    async def _spawn(self) -> None:
        """Start the child. On an OSError the child's pipe ends are closed here and
        this process's own ends are left to `_close_pipe`."""
        child_fds: list[int] = []
        try:
            read_fd, self._write_fd = os.pipe()
            child_fds.append(read_fd)
            fl = fcntl.fcntl(self._write_fd, fcntl.F_GETFL)
            fcntl.fcntl(self._write_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            self._send_deadline()
            self._alive_fd, alive_w = os.pipe()
            child_fds.append(alive_w)
            os.set_blocking(self._alive_fd, False)
            self._beat_at = time.monotonic()
            self._proc = await asyncio.create_subprocess_exec(
                *child_argv(read_fd, alive_w),
                pass_fds=(read_fd, alive_w),
                env=child_env(os.environ),
            )
        finally:
            for fd in child_fds:
                os.close(fd)
        asyncio.get_running_loop().add_reader(self._alive_fd, self._read_beats)
        log.info("workers child started: pid %s", self._proc.pid)
        self._publish = asyncio.get_running_loop().create_task(
            self._publish_loop(), name="loom-workers-child-lease"
        )

    def _send_deadline(self) -> None:
        """Send the lease deadline; it moves only while the lease is fresh."""
        if self._lease.fresh():
            self._deadline = time.monotonic() + self._lease.remaining_s()
        if self._write_fd is None:
            return
        with contextlib.suppress(BlockingIOError, BrokenPipeError, OSError):
            os.write(self._write_fd, f"{self._deadline!r}\n".encode())

    def _read_beats(self) -> None:
        assert self._alive_fd is not None
        try:
            got = os.read(self._alive_fd, 4096)
        except BlockingIOError:
            return
        if got:
            self._beat_at = time.monotonic()
        else:  # the child closed its end
            self._close_alive()

    def _close_alive(self) -> None:
        if self._alive_fd is not None:
            asyncio.get_running_loop().remove_reader(self._alive_fd)
            os.close(self._alive_fd)
            self._alive_fd = None

    async def _publish_loop(self) -> None:
        while True:
            self._send_deadline()
            await asyncio.sleep(PUBLISH_PERIOD_S)

    def _close_pipe(self) -> None:
        if self._publish is not None:
            self._publish.cancel()
            self._publish = None
        if self._write_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._write_fd)
            self._write_fd = None
        self._close_alive()

    async def _reap(self, kill_at: float) -> None:
        proc = self._proc
        try:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(
                        proc.wait(), timeout=max(0.0, kill_at - time.monotonic())
                    )
                except TimeoutError:
                    log.warning("workers child %s still running at its deadline; killing", proc.pid)
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()  # a cancelled stop
            if proc is not None:
                self.last_exit = proc.returncode
            self._close_pipe()


def child_argv(lease_fd: int, alive_fd: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "server.worker_main",
        "--lease-fd",
        str(lease_fd),
        "--alive-fd",
        str(alive_fd),
    ]


def child_env(parent: Mapping[str, str]) -> dict[str, str]:
    """The parent's environment, with the child's process shape: it runs the
    workers and never contends for the lease."""
    env = dict(parent)
    env["LOOM_BACKGROUND_WORKERS"] = "on"
    env["LOOM_WORKERS_PROCESS"] = "inline"
    return env


_RUNNER: WorkerSupervisor | ChildWorkers | None = None


def worker_runner(lease: leader.SupervisorLease) -> WorkerSupervisor | ChildWorkers:
    """What runs the workers here: a child process for an `auto` lease holder
    (`LOOM_WORKERS_PROCESS=child`), else the in-process supervisor."""
    global _RUNNER
    if lease.mode == "auto" and settings.workers_process == "child":
        _RUNNER = ChildWorkers(lease)
    else:
        _RUNNER = get_supervisor()
    return _RUNNER


def current_runner() -> WorkerSupervisor | ChildWorkers:
    return _RUNNER if _RUNNER is not None else get_supervisor()


# --- child side ---------------------------------------------------------------


class ChildLease:
    """The child's view of the parent's lease: `fresh()` until the deadline
    the parent last sent. `held` is False: the leader's duties (tracker rows,
    retention, the published config) stay with the parent."""

    def __init__(self) -> None:
        self.held = False
        self.error = ""
        self.given_up = ""
        self.reason = f"workers child of pid {os.getppid()}"
        self.deadline = time.monotonic()

    def remaining_s(self) -> float:
        return self.deadline - time.monotonic()

    def fresh(self) -> bool:
        return self.remaining_s() > 0


def read_deadlines(fd: int, lease: ChildLease, on_eof: Callable[[], None]) -> None:
    """Thread: keep `lease.deadline` at the last value the parent wrote. EOF
    means the parent exited: the lease is stale and the child stops."""
    with os.fdopen(fd, "rb") as pipe:
        for line in pipe:
            with contextlib.suppress(ValueError):
                lease.deadline = float(line)
    lease.deadline = min(lease.deadline, time.monotonic())
    on_eof()


def exit_past_deadline(lease: ChildLease, grace_s: float) -> None:
    """Thread: end the process once the deadline is `grace_s` past, whatever
    its shutdown is still doing (a parent stopped or gone renews nothing)."""
    while time.monotonic() <= lease.deadline + grace_s:
        time.sleep(0.5)
    log.error("workers child %s: lease deadline passed; exiting", os.getpid())
    os._exit(1)


async def beat(fd: int) -> None:
    """Write a beat to the parent every PUBLISH_PERIOD_S, from the event loop:
    a loop that hangs stops beating."""
    os.set_blocking(fd, False)
    while True:
        with contextlib.suppress(OSError):  # a full pipe, or the parent gone
            os.write(fd, b".")
        await asyncio.sleep(PUBLISH_PERIOD_S)


def die_with_parent() -> None:
    """The kernel kills this process when its parent dies (Linux only)."""
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
        log.warning(
            "workers child: prctl(PR_SET_PDEATHSIG) failed (errno %d); "
            "it outlives a parent that dies until its lease deadline passes",
            ctypes.get_errno(),
        )
