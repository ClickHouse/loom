"""A held descriptor is distinguishable from missing, remote, or dead proofs."""

import fcntl
import os
from uuid import uuid4

from server.core import provider_process as process


def test_missing_file_is_not_death(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "_directory", lambda: tmp_path)
    with process.stopped_process(str(uuid4())) as stopped:
        assert not stopped
    with process.stopped_process("not-a-process-id") as stopped:
        assert not stopped


def test_proof_requires_release_and_keeps_file(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "_directory", lambda: tmp_path)
    identity = uuid4()
    descriptor = os.open(tmp_path / identity.hex, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with process.stopped_process(str(identity)) as stopped:
            assert not stopped
    finally:
        os.close(descriptor)
    with process.stopped_process(str(identity)) as stopped:
        assert stopped
        with process.stopped_process(str(identity)) as another:
            assert not another
    assert (tmp_path / identity.hex).is_file()
