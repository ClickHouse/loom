"""Per-entry exclusion for the compiler-tier passes.

A full re-index (`loom code scip-full`) and a delta pass (`loom code
scip-delta`, the crossref pass, `run_python_delta.sh`) all move one entry's
checkout and write its watermarks, so two of them must never work on the same
entry at once. Different entries are independent and may run side by side.

Each entry has its own lock file, `<code_scip_lock_dir>/scip.<org>.<namespace>.lock`
(both names percent-encoded, dots included, so the name is unambiguous). The
lock is a whole-file `flock(2)`: `fcntl.flock` here and `flock(1)` in the host
cron scripts take the same kernel lock, so shell and Python exclude each other.
The kernel drops the lock when the holder exits, so a killed pass never leaves
a stale lock behind.

`python -m server.cli code scip-lock-path --org ORG --namespace NS` prints the
path for a shell caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

from server.settings import settings


class EntryLockHeld(Exception):
    """Another pass holds this entry's lock (non-blocking mode only)."""

    def __init__(self, org: str, namespace: str, path: Path) -> None:
        super().__init__(f"{org}/{namespace}: entry lock held ({path})")
        self.org = org
        self.namespace = namespace
        self.path = path


def _part(name: str) -> str:
    return quote(name, safe="").replace(".", "%2E")


def entry_lock_path(org: str, namespace: str) -> Path:
    """The lock file for one configured entry (relative settings resolve
    against the working directory, as the checkout paths do)."""
    return Path(settings.code_scip_lock_dir) / f"scip.{_part(org)}.{_part(namespace)}.lock"


def _acquire(path: Path, *, wait: bool) -> int | None:
    """Open and lock `path`; the fd, or None when non-blocking and held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.asynccontextmanager
async def entry_lock(org: str, namespace: str, *, wait: bool) -> AsyncIterator[Path]:
    """Hold the entry's lock for the block.

    `wait=False` raises `EntryLockHeld` at once when another pass holds it;
    `wait=True` blocks (in a thread, off the event loop) until it is free.
    """
    path = entry_lock_path(org, namespace)
    fd = await asyncio.to_thread(_acquire, path, wait=wait)
    if fd is None:
        raise EntryLockHeld(org, namespace, path)
    try:
        yield path
    finally:
        os.close(fd)  # closing the only fd releases the flock
