"""A small JSON document the web processes of one server share.

Runtime overrides set through one worker must bind its siblings. Each keeps its
own copy and `poll`s the file, which re-reads the small document; `update`
re-reads the latest version under an exclusive lock, bumps the document's
generation and replaces the file atomically, so concurrent updates never drop
each other's change. A sibling adopts a version whose generation it has not seen.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from typing import Any

from server.core.errors import OverrideBusyError, OverrideNotSharedError
from server.core.runtime_files import open_private, server_file

log = logging.getLogger(__name__)

# How long `waiting_out_siblings` retries an update a sibling's lock refused; an
# update holds the lock for one read and one atomic replace.
LOCK_WAIT_S = 1.0
# How often a poll asks again for a path that would not resolve; an update asks
# every time.
RESOLVE_RETRY_S = 5.0


class LockBusy(OSError):
    """A sibling web process holds the document's lock."""


class SharedDocument[T]:
    """`decode` turns the parsed JSON into the caller's state and `encode` turns
    the state back; a document `decode` cannot read is corrupt. `where` is the
    file's path, or finds it (`server_document`): while that raises OSError a
    poll keeps the current state and an update is refused."""

    def __init__(
        self,
        where: str | Callable[[], str],
        decode: Callable[[Any], T],
        encode: Callable[[T], object],
        *,
        name: str = "",
    ) -> None:
        # The file; None until `where` gives it, on first use.
        self.path: str | None = None
        self.name = name or (where if isinstance(where, str) else "shared document")
        self._where = where if callable(where) else lambda: where
        self._retry_at, self._unresolved = 0.0, ""
        self._decode, self._encode = decode, encode
        # The generation last adopted or published; None when none was.
        self._seen: int | None = None
        # The last read failure warned about, so a lasting one is logged once.
        self._warned = ""

    def _file(self, *, now: bool) -> str:
        """`path`, found on first use. Raises OSError while it cannot be; `now`
        asks again at once, else at most every RESOLVE_RETRY_S."""
        if self.path is not None:
            return self.path
        if not now and time.monotonic() < self._retry_at:
            raise OSError(self._unresolved)  # the same reason, so it is warned about once
        try:
            self.path = self._where()
        except OSError as e:
            self._retry_at, self._unresolved = time.monotonic() + RESOLVE_RETRY_S, str(e)
            raise
        return self.path

    def poll(self) -> T | None:
        """The document if it changed since the last poll or update, else None.
        A version that cannot be read is skipped with a warning."""
        try:
            return self._read_changed()
        except OSError as e:
            if str(e) != self._warned:
                self._warned = str(e)
                log.warning("%s unreadable (%s); keeping the current state", self.name, e)
            return None

    def update(self, change: Callable[[T], T], default: T) -> T:
        """Under the lock, publish and return `change(latest)`; `default` stands in
        when there is no document yet, or it is corrupt and this replaces it.
        Raises OSError, before anything is published, when it cannot be read or
        written."""
        path = self._file(now=True)
        lock = open_private(path + ".lock", os.O_RDWR | os.O_CREAT)
        try:
            _try_lock(lock, path)
            self._seen = None
            try:
                latest = self._read_changed()
                new = change(default if latest is None else latest)
                # A new or replaced corrupt document starts at the clock, a
                # generation no sibling can have seen.
                generation = time.time_ns() if self._seen is None else self._seen + 1
                _publish(path, {"generation": generation, "document": self._encode(new)})
                self._seen = generation
            except BaseException:
                self._seen = None  # the caller kept its state, so the next poll adopts `latest`
                raise
            return new
        finally:
            os.close(lock)

    def _read_changed(self) -> T | None:
        """The document when its generation is not the one last seen, else None.
        Missing or corrupt, it leaves no generation seen."""
        path = self._file(now=False)
        try:
            fd = open_private(path, os.O_RDONLY)
        except FileNotFoundError:
            self._seen = None
            return None
        try:
            fh = os.fdopen(fd, encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        try:
            with fh:
                raw = json.load(fh)  # an OSError here is not corruption: tried again
            generation = raw["generation"]
            if not isinstance(generation, int) or isinstance(generation, bool):
                raise TypeError(f"generation {generation!r} is not a whole number")
            if generation == self._seen:
                return None
            state = self._decode(raw["document"])
        except (ValueError, LookupError, TypeError, AttributeError, RecursionError) as e:
            if f"corrupt: {e}" != self._warned:
                self._warned = f"corrupt: {e}"
                log.warning("%s is corrupt (%s); ignored until it is replaced", path, e)
            self._seen = None
            return None
        self._seen = generation
        return state


def server_document[T](
    name: str, decode: Callable[[Any], T], encode: Callable[[T], object]
) -> SharedDocument[T]:
    """The document `name` this server's web processes share, in the runtime
    directory (`server_file`), found on first use: until a private directory
    exists, updates answer OVERRIDE_NOT_SHARED and each process keeps its own."""
    return SharedDocument(lambda: server_file(name) + ".json", decode, encode, name=name)


def share_override[T](
    doc: SharedDocument[T] | None, change: Callable[[T], T], current: T, *, what: str
) -> T:
    """`change(current)`, published to the server's other web processes through
    `doc` (None: this process is the only one). Raises OverrideBusyError when a
    sibling's update holds the lock (a retry goes through), OverrideNotSharedError
    when the document cannot be shared at all; either way nothing changed."""
    if doc is None:
        return change(current)
    try:
        return doc.update(change, current)
    except LockBusy as e:
        raise OverrideBusyError(f"{what} not applied: another web process is updating it") from e
    except OSError as e:
        log.warning("%s not shared: %s", what, e)
        raise OverrideNotSharedError(
            f"{what} not applied: the server's web processes could not share it"
        ) from e


async def waiting_out_siblings[**P, R](
    update: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> R:
    """`update(...)`, tried again for up to LOCK_WAIT_S while a sibling's update
    holds the document's lock, awaiting between tries so the event loop serves
    other requests. For updates that set values, so a retry repeats nothing."""
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            return update(*args, **kwargs)
        except OverrideBusyError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.005)


def _try_lock(fd: int, path: str) -> None:
    """Take the exclusive lock, or raise LockBusy at once: an update runs on the
    event loop, so it never sleeps there (`waiting_out_siblings` retries it)."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise LockBusy(errno.EWOULDBLOCK, f"{path}.lock is held by another process") from None


def _publish(path: str, data: object) -> None:
    """Replace `path` atomically."""
    # Named after the document, so a file left by a killed worker is pruned with it.
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=f"{os.path.basename(path)}.tmp-"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
