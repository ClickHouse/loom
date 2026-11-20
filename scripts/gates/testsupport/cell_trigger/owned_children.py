"""Reap only exact direct and adopted children of the fixture subreaper."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
from pathlib import Path


class OwnedChildren:
    """Signal only unreaped kernel children of this private subreaper."""

    def __init__(self) -> None:
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("exact owned-process signaling needs Linux pidfds")
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "PR_SET_CHILD_SUBREAPER: " + os.strerror(error))
        self.pid = os.getpid()

    @staticmethod
    def identity(pid: int) -> tuple[int, str, str] | None:
        try:
            fields = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
            return int(fields[1]), fields[19], fields[0]
        except (FileNotFoundError, ProcessLookupError):
            return None

    @staticmethod
    def children(pid: int) -> list[int]:
        return [
            int(value)
            for value in Path("/proc", str(pid), "task", str(pid), "children").read_text().split()
        ]

    @staticmethod
    def rss(pid: int) -> int:
        try:
            status = Path("/proc", str(pid), "status").read_text()
        except FileNotFoundError:
            return 0  # Vanished sample, never proof that the process used no memory.
        return next(
            (int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:")), 0
        )

    def stop(self, process: subprocess.Popen | None, note: object) -> None:
        started = None
        last_note = None
        diagnostic_error = None
        # Keep each verified pidfd pinned, including across numeric PID reuse.
        pinned: dict[int, tuple[int, set[int]]] = {}

        def cleanup_note(**details: object) -> None:
            nonlocal diagnostic_error
            try:
                note("cleanup-wait", **details)
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                diagnostic_error = error

        try:
            while True:
                try:
                    children = self.children(self.pid)
                    for pid in children:
                        row = self.identity(pid)
                        if row and row[0] == self.pid and row[2] == "Z":
                            if (
                                process is not None
                                and process.returncode is None
                                and pid == process.pid
                            ):
                                process.poll()  # Preserve the real leader exit status.
                            else:
                                os.waitpid(pid, os.WNOHANG)
                    children = self.children(self.pid)
                    if not children:
                        break  # Kernel list is empty AFTER zombie reaping.
                    if started is None:
                        started = last_note = time.monotonic()
                    elapsed = time.monotonic() - started
                    sig = (
                        signal.SIGINT
                        if elapsed < 30
                        else (signal.SIGTERM if elapsed < 40 else signal.SIGKILL)
                    )
                    for pid in children:
                        row = self.identity(pid)
                        if row is None or row[0] != self.pid or row[2] == "Z":
                            continue
                        if pid in pinned:
                            descriptor, sent = pinned[pid]
                            try:
                                signal.pidfd_send_signal(descriptor, 0)
                            except ProcessLookupError:
                                os.close(descriptor)
                                del pinned[pid]
                        if pid not in pinned:
                            try:
                                descriptor = os.pidfd_open(pid)
                            except ProcessLookupError:
                                continue
                            try:
                                exact = self.identity(pid)
                                if exact is None or exact[0] != self.pid or exact[1] != row[1]:
                                    os.close(descriptor)
                                    continue
                            except BaseException:
                                os.close(descriptor)
                                raise
                            pinned[pid] = (descriptor, set())
                        descriptor, sent = pinned[pid]
                        if int(sig) not in sent:
                            try:
                                signal.pidfd_send_signal(descriptor, sig)
                                sent.add(int(sig))
                            except ProcessLookupError:
                                pass
                    if time.monotonic() - last_note >= 30:
                        cleanup_note(owned_direct_pids=children)
                        last_note = time.monotonic()
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    # Unknown census/signal state retains exclusion until the kernel
                    # proves that every direct/adopted child has been reaped.
                    if diagnostic_error is None:
                        cleanup_note(
                            reason="owned-child cleanup failed", error=type(error).__name__
                        )
                    diagnostic_error = error
                try:
                    time.sleep(0.1)
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    # Even a sleep/clock failure cannot escape over an unreaped child.
                    diagnostic_error = error
            if process is not None:
                process.wait()
        finally:
            for descriptor, _sent in pinned.values():
                os.close(descriptor)
        if diagnostic_error is not None:
            raise RuntimeError(
                "cleanup failed; kernel children are now empty and reaped"
            ) from diagnostic_error
