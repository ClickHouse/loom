"""loom.process.open_fds / fd_limit read the kernel, not the store."""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

from server.observability import metrics_gauges


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux")
def test_process_fd_reading_counts_this_process_and_moves_with_an_open_file() -> None:
    # Other threads (the event loop, GC finalizers, a parallel runner) open and
    # close descriptors between readings, so exact equality races; ten files of
    # our own move the count by at least ten and closing them moves it back.
    open_before, limit = metrics_gauges.process_fd_reading()
    assert open_before >= 3  # stdin, stdout, stderr at least
    assert limit >= open_before
    files = [tempfile.TemporaryFile() for _ in range(10)]
    try:
        open_during, _ = metrics_gauges.process_fd_reading()
    finally:
        for f in files:
            f.close()
    open_after, _ = metrics_gauges.process_fd_reading()
    assert open_during - open_before >= 8, (open_before, open_during)
    assert open_during - open_after >= 8, (open_during, open_after)


def test_process_fd_reading_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_path: str) -> list[str]:
        raise FileNotFoundError("/proc/self/fd")

    monkeypatch.setattr(os, "listdir", _boom)
    open_fds, limit = metrics_gauges.process_fd_reading()
    assert open_fds == -1 and (limit == -1 or limit > 0)


def test_data_disk_reading_reports_free_within_total(tmp_path: object) -> None:
    free, total = metrics_gauges.data_disk_reading(str(tmp_path))
    assert total > 0 and 0 <= free <= total


def test_data_disk_reading_falls_back_to_the_working_directory() -> None:
    free, total = metrics_gauges.data_disk_reading("/nonexistent/var")
    assert total > 0 and 0 <= free <= total


def test_data_disk_reading_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_path: str) -> object:
        raise OSError("statvfs")

    monkeypatch.setattr(os, "statvfs", _boom)
    assert metrics_gauges.data_disk_reading(".") == (-1, -1)
