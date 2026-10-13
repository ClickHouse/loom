"""A small JSON document the web processes of one server share.

Runtime overrides set through one worker must bind its siblings. Each keeps its
own copy and `poll`s the file, which costs one `lstat` when nothing changed;
`update` re-reads the latest version under an exclusive lock and replaces the
file atomically, so concurrent updates never drop each other's change.
"""

from __future__ import annotations

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

from server.core.errors import OverrideNotSharedError
from server.core.runtime_files import open_private

log = logging.getLogger(__name__)

_Version = tuple[int, int, int]  # inode, mtime_ns, size
# How long an update waits for a sibling's lock; an update holds it for one
# read and one atomic replace.
LOCK_WAIT_S = 1.0


def _version(st: os.stat_result) -> _Version:
    return (st.st_ino, st.st_mtime_ns, st.st_size)


class SharedDocument[T]:
    """`decode` turns the parsed JSON into the caller's state and `encode` turns
    the state back; a document `decode` cannot read is corrupt."""

    def __init__(
        self, path: str, decode: Callable[[Any], T], encode: Callable[[T], object]
    ) -> None:
        self.path = path
        self._decode, self._encode = decode, encode
        # The version last adopted, published or found corrupt.
        self._seen: _Version | None = None
        # The last read failure warned about, so a lasting one is logged once.
        self._warned = ""

    def poll(self) -> T | None:
        """The document if it changed since the last poll or update, else None.
        A version that cannot be read is skipped with a warning."""
        try:
            return self._read_changed()
        except OSError as e:
            if str(e) != self._warned:
                self._warned = str(e)
                log.warning("%s unreadable (%s); keeping the current state", self.path, e)
            return None

    def update(self, change: Callable[[T], T], default: T) -> T:
        """Under the lock, publish and return `change(latest)`; `default` stands in
        when there is no document yet, or it is corrupt and this replaces it.
        Raises OSError, before anything is published, when it cannot be read or
        written."""
        lock = open_private(self.path + ".lock", os.O_RDWR | os.O_CREAT)
        try:
            _lock_within(lock, self.path, LOCK_WAIT_S)
            self._seen = None
            try:
                latest = self._read_changed()
                new = change(default if latest is None else latest)
                self._seen = _publish(self.path, self._encode(new))
            except BaseException:
                self._seen = None  # the caller kept its state, so the next poll adopts `latest`
                raise
            return new
        finally:
            os.close(lock)

    def _read_changed(self) -> T | None:
        try:
            version = _version(os.lstat(self.path))
        except FileNotFoundError:
            return None
        if version == self._seen:
            return None
        try:
            fd = open_private(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, encoding="utf-8") as fh:
            version = _version(os.fstat(fh.fileno()))
            try:
                state = self._decode(json.load(fh))
            except (ValueError, LookupError, TypeError, AttributeError) as e:
                log.warning("%s is corrupt (%s); ignored until it is replaced", self.path, e)
                state = None
        # Seen once read or found corrupt; a read that failed is tried again.
        self._seen = version
        return state


def share_override[T](
    doc: SharedDocument[T] | None, change: Callable[[T], T], current: T, *, what: str
) -> T:
    """`change(current)`, published to the server's other web processes through
    `doc` (None: this process is the only one). Raises OverrideNotSharedError,
    having changed nothing, when it cannot be shared; the log says why."""
    if doc is None:
        return change(current)
    try:
        return doc.update(change, current)
    except OSError as e:
        log.warning("%s not shared: %s", what, e)
        raise OverrideNotSharedError(
            f"{what} not applied: the server's web processes could not share it"
        ) from e


def _lock_within(fd: int, path: str, wait_s: float) -> None:
    """Take the exclusive lock, or raise OSError after `wait_s`. An update runs on
    the event loop, so a sibling stuck holding the lock stalls it for `wait_s` at
    most; a sibling's own update holds it for one read and one replace."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise OSError(errno.EWOULDBLOCK, f"{path}.lock held past {wait_s:g} s") from None
            time.sleep(0.002)


def _publish(path: str, data: object) -> _Version:
    """Replace `path` atomically. The version comes from the written file before
    the rename (which keeps it), so nothing can fail once the rename commits."""
    # Named after the document, so a file left by a killed worker is pruned with it.
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=f"{os.path.basename(path)}.tmp-"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            version = _version(os.fstat(fh.fileno()))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return version
