"""A small JSON document the web processes of one server share.

Runtime overrides set through one worker must bind its siblings. Each keeps its
own copy and `poll`s the file, which costs one `lstat` when nothing changed;
`update` re-reads the latest version under an exclusive lock and replaces the
file atomically, so concurrent updates never drop each other's change.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import stat
import tempfile
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

FILE_MODE = 0o600
_Version = tuple[int, int, int]  # inode, mtime_ns, size


def open_private(path: str, flags: int) -> int:
    """Open `path` never through a symlink, as a regular file owned by this
    process, created (with O_CREAT) or tightened to 0600. Raises OSError."""
    fd = os.open(path, flags | os.O_NOFOLLOW, FILE_MODE)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path}: not a regular file")
        if st.st_uid != os.geteuid():
            raise OSError(f"{path}: owned by uid {st.st_uid}, this process is {os.geteuid()}")
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.fchmod(fd, FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _version(st: os.stat_result) -> _Version:
    return (st.st_ino, st.st_mtime_ns, st.st_size)


class SharedDocument[T]:
    """`decode` turns the parsed JSON into the caller's state; a document it
    rejects (ValueError, KeyError, TypeError) is corrupt."""

    def __init__(self, path: str, decode: Callable[[Any], T]) -> None:
        self.path = path
        self._decode = decode
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

    def update(self, change: Callable[[T | None], T], encode: Callable[[T], object]) -> T:
        """Under the lock, publish and return `change(latest)`. `latest` is None when
        there is no document yet, or it is corrupt and this replaces it. Raises
        OSError, before anything is published, when it cannot be read or written."""
        lock = open_private(self.path + ".lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._seen = None
            new = change(self._read_changed())
            self._seen = _publish(self.path, encode(new))
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
            self._seen = _version(os.fstat(fh.fileno()))
            try:
                return self._decode(json.load(fh))
            except (ValueError, KeyError, TypeError) as e:
                log.warning("%s is corrupt (%s); ignored until it is replaced", self.path, e)
                return None


def _publish(path: str, data: object) -> _Version:
    """Replace `path` atomically. The version comes from the written file before
    the rename (which keeps it), so nothing can fail once the rename commits."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-")
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
