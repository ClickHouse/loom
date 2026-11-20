"""Read a file the server does not control: a checkout's manifest or a sandboxed job's output.

A repository decides what its checkout holds, and a sandboxed job decides what its output
directory holds, so either can put a symlink (to /dev/zero, to the server's own files), a
FIFO or a huge file where the server reads a small one. `open_regular` opens without
following a final symlink and only a regular file within its cap; `read_bounded` then
reads it whole, never more than one byte past the cap. A compiler-tier delta is read
through `open_regular` a value at a time (`delta_file`).
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

# not a setting: a lockfile is only searched as bytes, so this bounds what the host holds
CHECKOUT_FILE_MAX_BYTES = 64 * 1024 * 1024
# not a setting: a package.json is parsed at up to ~30x its bytes; real ones are a few KiB
PACKAGE_JSON_MAX_BYTES = 1024 * 1024


class FileRefused(OSError):
    """The file is a symlink, not a regular file, over its cap, or changed while read."""


def open_regular(path: Path, max_bytes: int) -> tuple[int, os.stat_result]:
    """An open descriptor of the regular file at `path` and its stat, refused when it is
    a link, not a regular file, or larger than `max_bytes`. The caller closes it."""
    # O_NONBLOCK: opening a FIFO must not wait for a writer; fstat refuses it next.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise FileRefused(f"{path} is a symlink or a symlink loop; not read") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FileRefused(f"{path} is not a regular file; not read")
        if st.st_size > max_bytes:
            raise FileRefused(f"{path} is {st.st_size} bytes; max {max_bytes}")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def read_bounded(path: Path, max_bytes: int) -> bytearray:
    """The bytes of the regular file at `path`, refused when it is larger than `max_bytes`."""
    fd, st = open_regular(path, max_bytes)
    size = st.st_size
    try:
        # Read into one buffer, no copy; a byte past the stated size shows a file that grew.
        data = bytearray(size + 1)
        got = 0
        with memoryview(data) as view:
            while got < len(data) and (n := os.readv(fd, [view[got:]])):
                got += n
    finally:
        os.close(fd)
    if got != size:
        raise FileRefused(f"{path} changed while it was read")
    del data[got:]  # the spare byte; shrinks in place
    return data


def read_checkout_file(path: Path) -> bytearray:
    """A lockfile the host searches from a repository's checkout, never parsed."""
    return read_bounded(path, CHECKOUT_FILE_MAX_BYTES)


def read_package_json(path: Path) -> bytearray:
    """A checkout's package.json, small enough to parse."""
    return read_bounded(path, PACKAGE_JSON_MAX_BYTES)
