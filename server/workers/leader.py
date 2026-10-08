"""One supervisor per host.

`uvicorn --workers N` forks N control-plane processes, and each runs the
lifespan; without an election each would run every background pass.

This module elects one process per host with an advisory file lock. `flock`
is per open file description, so the first process to open and lock the
path wins and keeps the fd for its lifetime; a sibling gets EWOULDBLOCK and
skips the supervisor; if the leader dies the kernel drops the lock and the
process uvicorn respawns acquires it on its next boot. No ClickHouse table,
no lease renewal, no clock — the kernel is the arbiter, and the scope (one
host) is exactly the scope the fork created.

Modes (`LOOM_BACKGROUND_WORKERS`):
  auto  — take the lock; run the supervisor only if it was acquired (default)
  on    — always run (single process, or an operator who wants N copies)
  off   — never run (a pure web replica behind a load balancer)
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("loom.workers.leader")


@dataclass
class SupervisorLease:
    """Holds the lock fd for the process lifetime; `held` says whether this
    process runs the supervisor and `reason` says why, for /v1/health."""

    path: str
    held: bool = False
    reason: str = ""
    _fd: int | None = None

    def acquire(self) -> bool:
        try:
            import fcntl
        except ImportError:  # pragma: no cover — non-POSIX: no fork either
            self.held, self.reason = True, "no flock on this platform; assuming one process"
            return True
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            # A lock path we cannot open must not silence the workers: fail
            # open (run) and say so, rather than leave a host with no supervisor.
            self.held, self.reason = True, f"lock path unavailable ({e}); running"
            log.warning("supervisor lease: %s", self.reason)
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            self.held, self.reason = False, f"another process holds {self.path}"
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass
        self._fd = fd
        self.held, self.reason = True, f"holds {self.path} (pid {os.getpid()})"
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001 — the close below drops the lock regardless
            pass
        try:
            os.close(self._fd)
        finally:
            self._fd = None
            self.held = False


def decide_supervisor(mode: str, lock_path: str) -> SupervisorLease:
    """The one call the lifespan makes: a lease whose `held` says whether to
    start the supervisor in this process."""
    lease = SupervisorLease(path=lock_path)
    if mode == "on":
        lease.held, lease.reason = True, "LOOM_BACKGROUND_WORKERS=on"
    elif mode == "off":
        lease.held, lease.reason = False, "LOOM_BACKGROUND_WORKERS=off"
    else:
        lease.acquire()
    return lease


async def retry_until_held(
    lease: SupervisorLease,
    interval_s: float,
    on_acquired: Callable[[], None],
) -> None:
    """Keep trying for the lease and start the supervisor when it comes free.

    Under a blue/green deploy the two colours share the lock
    path, so the new colour boots while the old one still holds the lease.
    The old colour exits a few seconds later and, without this loop, nothing
    on the host runs the workers until the next restart. `auto` mode only;
    `interval_s <= 0` disables the retry (one try at boot). Cancelled by the lifespan on shutdown.
    """
    if interval_s <= 0:
        return
    while not lease.held:
        await asyncio.sleep(interval_s)
        if lease.acquire():
            log.info("supervisor lease: acquired after retry — %s", lease.reason)
            on_acquired()
            return


_CURRENT: SupervisorLease | None = None


def set_current(lease: SupervisorLease) -> None:
    global _CURRENT
    _CURRENT = lease


def current() -> SupervisorLease | None:
    return _CURRENT


__all__ = ["SupervisorLease", "current", "decide_supervisor", "retry_until_held", "set_current"]
