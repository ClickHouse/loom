"""Exact single-thread process exit is provable before its parent collects it."""

import ctypes
import os
import select
import signal
import threading
import time
from pathlib import Path

import pytest

from server.core import provider_process

pytestmark = pytest.mark.skipif(not provider_process.host_proof(), reason="needs /proc")


def _stat(pid: int) -> tuple[str, int, str, int]:
    parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return parts[0], int(parts[1]), parts[19], int(parts[17])


def _wait_owned(pid: int, start: str) -> int:
    """Collect only this fork's child, within a finite cleanup allowance."""
    for force in (False, True):
        end = time.monotonic() + 2.0
        if force:
            try:
                _, parent, now_start, _ = _stat(pid)
            except FileNotFoundError:
                parent, now_start = None, None
            if parent == os.getpid() and now_start == start:
                os.kill(pid, signal.SIGKILL)
        while time.monotonic() < end:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found == pid:
                return status
            time.sleep(0.005)
    raise AssertionError("Owned witness child did not settle within cleanup allowance")


def test_an_exact_single_thread_zombie_is_proven_stopped_before_wait() -> None:
    """A captured child has exited, even while its parent retains the wait status."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)
        os.read(read_fd, 1)
        os._exit(0)
    os.close(read_fd)
    start = None
    released = False
    try:
        state, parent, start, threads = _stat(pid)
        assert state != "Z" and parent == os.getpid() and threads == 1
        proof = provider_process.host_proof().split("|")
        assert len(proof) == 8 and all(proof[i] for i in (0, 1, 3))
        proof[4:6] = [str(pid), start]
        exact = "|".join(proof)
        assert not provider_process.proves_stopped(exact), "Live child must refuse"
        wrong_namespace = proof.copy()
        wrong_namespace[3] = str(int(proof[3]) + 1)
        assert not provider_process.proves_stopped("|".join(wrong_namespace))
        os.write(write_fd, b"x")
        released = True
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            state, parent, observed_start, threads = _stat(pid)
            assert parent == os.getpid() and observed_start == start
            if state == "Z" and threads == 1:
                break
            time.sleep(0.005)
        assert state == "Z" and threads == 1, (
            "Controlled child must reach single-thread Z before any waitpid"
        )
        assert provider_process.proves_stopped(exact)
    finally:
        if not released:
            os.write(write_fd, b"x")
        os.close(write_fd)
        if start is None:
            _, parent, start, _ = _stat(pid)
            assert parent == os.getpid()
        status = _wait_owned(pid, start)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert provider_process.proves_stopped(exact), "After waitpid, absence proves death"


def test_a_zombie_leader_is_refused_until_its_last_live_thread_exits() -> None:
    """Z alone is insufficient while another thread of that exact process runs."""
    read_fd, write_fd = os.pipe()
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)
        os.close(ready_read)

        def live_thread() -> None:
            os.write(ready_write, b"r")
            os.read(read_fd, 1)
            os._exit(0)

        threading.Thread(target=live_thread).start()
        libc = ctypes.CDLL(None)
        libc.pthread_exit.argtypes = [ctypes.c_void_p]
        libc.pthread_exit.restype = None
        libc.pthread_exit(None)
        os._exit(99)  # pthread_exit must not return.
    os.close(read_fd)
    os.close(ready_write)
    start = None
    released = False
    try:
        _, parent, start, _ = _stat(pid)
        assert parent == os.getpid()
        proof = provider_process.host_proof().split("|")
        assert len(proof) == 8 and all(proof[i] for i in (0, 1, 3))
        proof[4:6] = [str(pid), start]
        exact = "|".join(proof)
        assert select.select([ready_read], [], [], 2.0)[0] == [ready_read]
        assert os.read(ready_read, 1) == b"r"
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            state, parent, observed_start, threads = _stat(pid)
            assert parent == os.getpid() and observed_start == start
            if state == "Z" and threads > 1:
                break
            time.sleep(0.005)
        assert state == "Z" and threads > 1, "Live-thread leader-Z witness was not established"
        assert not provider_process.proves_stopped(exact)
        os.write(write_fd, b"x")
        released = True
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            state, parent, observed_start, threads = _stat(pid)
            assert parent == os.getpid() and observed_start == start
            if state == "Z" and threads == 1:
                break
            time.sleep(0.005)
        assert state == "Z" and threads == 1, "Controlled group exit must reach Z1 before waitpid"
        assert provider_process.proves_stopped(exact)
    finally:
        if not released:
            os.write(write_fd, b"x")
        os.close(write_fd)
        os.close(ready_read)
        if start is None:
            _, parent, start, _ = _stat(pid)
            assert parent == os.getpid()
        status = _wait_owned(pid, start)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert provider_process.proves_stopped(exact)
