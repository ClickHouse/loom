"""The lock files beside a bare mirror: whole-file `flock(2)`s that keep the
processes of one host apart (`.clone-lock` for a clone, `.fetch-lock` for every
fetch, `.gc-lock` for gc). The kernel drops a lock when its holder exits. Also
the name of a clone's temp tree, `<mirror>.partial-<pid>-<ns>`."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
import time
from collections.abc import AsyncIterator

from server.code.app.index.repo_modes import lock_file_mode

# A clone's temp tree beside its mirror: `<mirror>.partial-<pid>-<monotonic ns>`.
_PARTIAL = re.compile(r"\A(.+)\.partial-\d+-\d+")  # fullmatch only


def partial_temp_dir(git_dir: str) -> str:
    """A new temp tree name for a clone of `git_dir`."""
    return f"{git_dir}.partial-{os.getpid()}-{time.monotonic_ns()}"


def partial_mirror(path: str) -> str | None:
    """The mirror a clone's temp tree belongs to; None for any other path."""
    m = _PARTIAL.fullmatch(os.path.basename(path))
    return os.path.join(os.path.dirname(path), m.group(1)) if m else None


def try_lock(path: str) -> int | None:
    """An fd holding an exclusive lock on `path`, None when another process holds it."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, lock_file_mode(path))
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _prepare_lock(path: str) -> int:
    """Make the lock's directory and return the mode to create the lock file `path` at."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return lock_file_mode(path)


async def lock_file(path: str) -> int:
    """An fd holding an exclusive lock on `path`, polled for, so a cancelled
    wait holds nothing."""
    mode = await asyncio.to_thread(_prepare_lock, path)
    # On the loop, as entry_lock opens its own: an open in a thread a cancellation
    # abandons would leak its fd.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, mode)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                await asyncio.sleep(0.5)
    except BaseException:
        os.close(fd)
        raise


def fetch_lock_path(git_dir: str) -> str:
    return f"{git_dir}.fetch-lock"


@contextlib.asynccontextmanager
async def fetch_lock(git_dir: str) -> AsyncIterator[None]:
    """Held by every fetch into the mirror (the pass, the host loop, a read's fetch,
    a clone's pins), so two processes of the host never update its refs at once."""
    fd = await lock_file(fetch_lock_path(git_dir))
    try:
        yield
    finally:
        os.close(fd)
