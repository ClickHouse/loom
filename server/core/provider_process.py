"""Kernel-held process proofs, not heartbeat-based guesses about dead writers.

Files live on the persistent blob volume. A missing file proves nothing (it
may belong to another host). Acquiring an existing file's lock proves that its
original process released the descriptor. Never unlink these proof files.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from server.settings import settings

_descriptor: int | None = None
_identity: UUID | None = None


def _directory() -> Path:
    return Path(settings.blob_dir) / "provider-processes"


def process_identity() -> UUID:
    """Hold the proof until process exit, including shutdown's deferred writes."""
    global _descriptor, _identity
    if _identity is None:
        directory = _directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = uuid4()
        descriptor = os.open(directory / identity.hex, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _descriptor, _identity = descriptor, identity
    return _identity


@contextmanager
def stopped_process(identity: str) -> Iterator[bool]:
    """Keep the death proof locked through retirement and lock recovery."""
    try:
        name = UUID(identity).hex
    except ValueError:
        yield False
        return
    try:
        descriptor = os.open(_directory() / name, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield False
        return
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        os.close(descriptor)
