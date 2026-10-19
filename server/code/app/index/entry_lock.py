"""Per-entry exclusion for the compiler-tier passes.

A full re-index (code_scip_full, `loom code scip-full`) and a delta pass
(code_scip_delta, `loom code scip-delta`, `run_python_delta.sh`) all move one
entry's checkout and write its watermarks, so two of them must never work on the
same entry at once. Different entries are independent and may run side by side.
The Python entry points take the lock themselves; a harness run by hand goes
under `loom code entry-lock --org ORG --namespace NS -- <command>`.

Two locks, taken in this order:

- The host's: a lock file per entry, `<code_scip_lock_dir>/scip.<org>.<namespace>.lock`
  (both names percent-encoded, dots included, so the name is unambiguous), a
  whole-file `flock(2)`, which keeps one host's passes apart. The kernel drops
  it when the holder exits.
- The deployment's: the lease `scip_entry:<org>/<namespace>` (`server.core.leases`),
  so two hosts never run one entry at once. It is renewed while held and expires
  `ENTRY_LEASE_TTL_S` after its holder dies. When it cannot be taken (ClickHouse or
  Keeper unreachable) the pass fails: the host's file alone excludes nothing on
  another host.

A holder counts the lease lost half the TTL after its last renewal, a pause
included, before another host can take it over; the block is then cancelled and
`EntryLockLost` raised. Every publishing write of a pass (index rows, watermarks)
calls `check_held()` first, so a pass that lost its lease writes nothing more, even
before the cancellation reaches it; the `entry-lock` wrapper kills its command's
process group. There are no fencing tokens: a statement already sent lands, and has
at least the other half of the TTL to do so. The wrapper hands its command the
lease's holder (`HOLDER_ENV`); an upload that names it (`code.scip_ingest`'s
`entry_lease_holder`) is refused once another holder has the entry, so a batch
completes only while its pass held the lease.

`python -m server.cli code scip-lock-path --org ORG --namespace NS` prints the
file's path.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import fcntl
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

from server.core import leases
from server.settings import settings

# A dead holder's lease is taken over after this long.
ENTRY_LEASE_TTL_S = 120
# The environment variable the `entry-lock` wrapper hands its command the holder in.
HOLDER_ENV = "LOOM_ENTRY_LEASE_HOLDER"
# How often a waiting pass retries the host's lock file.
_POLL_S = 1.0


class EntryLockHeld(Exception):
    """Another pass holds this entry's lock (non-blocking mode only)."""

    def __init__(self, org: str, namespace: str, path: Path | str) -> None:
        super().__init__(f"{org}/{namespace}: entry lock held ({path})")
        self.org = org
        self.namespace = namespace
        self.path = path


class EntryLockUnavailable(Exception):
    """The deployment-wide lock could not be taken (ClickHouse or Keeper
    unreachable, a refused statement): not known to be held, so the pass is an
    error, not a skip, and does not run."""


class EntryLockLost(Exception):
    """The entry's lease was lost while the pass held it: another host may
    run this entry now, so this pass writes nothing further."""


def lease_lost() -> bool:
    """The enclosing `entry_lock` block's lease was lost (False outside one)."""
    lease = _current.get()
    return lease is not None and not lease.held


def current_holder() -> str:
    """The holder of the enclosing `entry_lock` block's lease ('' outside one)."""
    lease = _current.get()
    return lease.holder if lease is not None else ""


def check_held() -> None:
    """Raise `EntryLockLost` when the entry lease of the enclosing
    `entry_lock` block was lost. No ClickHouse call; outside a block, nothing."""
    if lease_lost():
        raise EntryLockLost("entry lease lost")


def _part(name: str) -> str:
    return quote(name, safe="").replace(".", "%2E")


def entry_lock_path(org: str, namespace: str) -> Path:
    """The lock file for one configured entry (relative settings resolve
    against the working directory, as the checkout paths do)."""
    return Path(settings.code_scip_lock_dir) / f"scip.{_part(org)}.{_part(namespace)}.lock"


def entry_lease_name(org: str, namespace: str) -> str:
    return f"scip_entry:{_part(org)}/{_part(namespace)}"


async def _acquire(path: Path, *, wait: bool) -> int | None:
    """Open and lock `path`; the fd, or None when non-blocking and held. A wait
    polls, so a cancelled one holds nothing."""
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if not wait:
                    os.close(fd)
                    return None
                await asyncio.sleep(_POLL_S)
    except BaseException:
        os.close(fd)
        raise


# The lease of the entry lock block the current task is in.
_current: contextvars.ContextVar[leases.Lease | None] = contextvars.ContextVar(
    "loom_entry_lease", default=None
)


async def _take_lease(org: str, namespace: str, *, wait: bool) -> leases.Lease:
    """The entry's deployment-wide lease. `EntryLockHeld` when another process holds
    it, `EntryLockUnavailable` when it could not be taken."""
    name = entry_lease_name(org, namespace)
    try:
        lease = await leases.take(
            name,
            ttl_s=ENTRY_LEASE_TTL_S,
            wait_s=settings.code_entry_lock_wait_s if wait else 0.0,
            poll_s=2.0,
            poll_max_s=30.0,
        )
    except leases.LeaseUnavailable as e:
        raise EntryLockUnavailable(f"entry lease {name}: {e.__cause__ or e}") from e
    if lease is None:
        raise EntryLockHeld(org, namespace, name)
    return lease


@contextlib.asynccontextmanager
async def entry_lock(org: str, namespace: str, *, wait: bool) -> AsyncIterator[Path]:
    """Hold the entry's lock for the block.

    `wait=False` raises `EntryLockHeld` at once when another pass holds it;
    `wait=True` waits until it is free.
    """
    path = entry_lock_path(org, namespace)
    fd = await _acquire(path, wait=wait)
    if fd is None:
        raise EntryLockHeld(org, namespace, path)
    try:
        lease = await _take_lease(org, namespace, wait=wait)
        token = _current.set(lease)
        try:
            async with lease.renewed():
                yield path
        except leases.LeaseLost as e:
            raise EntryLockLost(f"entry lease {lease.name} lost") from e
        finally:
            with contextlib.suppress(ValueError):  # closed from another context
                _current.reset(token)
    finally:
        os.close(fd)  # closing the only fd releases the flock
