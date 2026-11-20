"""Bounded native restore phase evidence that survives pytest capture."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

_DIRECTORY = Path(__file__).resolve().parent
_MODULES = {"test_cell_restore.py", "test_cell_restore_reconcile.py"}
_SEQUENCE = pytest.StashKey[int]()
_MAX_RECORDS = 4096
# Zero means another exception type; exception messages and dynamic names stay private.
_TYPES = {
    "AssertionError": 1,
    "TimeoutError": 2,
    "ValueError": 3,
    "RuntimeError": 4,
    "TypeError": 5,
    "OSError": 6,
    "CancelledError": 7,
    "SystemExit": 8,
    "KeyboardInterrupt": 9,
    "Failed": 10,
    "Skipped": 11,
    "XFailed": 12,
}


def _location(error: BaseException | None, root: Path) -> tuple[str, int]:
    found = ("", 0)
    trace = error.__traceback__ if error is not None else None
    for _ in range(128):
        if trace is None:
            return found
        path = Path(trace.tb_frame.f_code.co_filename)
        if path.is_relative_to(root) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,79}\.py", path.name
        ):
            found = (path.name, trace.tb_lineno)
        trace = trace.tb_next
    return "", 0


def _record(item: pytest.Item, phase: int, event: int, error: BaseException | None = None) -> None:
    try:
        count = item.config.stash.get(_SEQUENCE, 0)
        if count > _MAX_RECORDS:
            return
        item.config.stash[_SEQUENCE] = count + 1
        filename, line = _location(error, item.config.rootpath)
        record = {
            "v": 1,
            "seq": count + 1,
            "node": hashlib.sha256(item.nodeid.encode()).hexdigest(),
            "phase": phase,  # setup=0, call=1, teardown=2
            # Events: enter=0, return=1, raise=2, overflow=3.
            "event": 3 if count == _MAX_RECORDS else event,
            "monotonic_ns": time.monotonic_ns(),
            "type": _TYPES.get(type(error).__name__, 0) if error is not None else 0,
            "file": filename,
            "line": line,
        }
        reporter = item.config.pluginmanager.getplugin("terminalreporter")
        capture = item.config.pluginmanager.getplugin("capturemanager")
        if reporter is not None and capture is not None:
            with capture.global_and_fixture_disabled():
                reporter.write_line("RESTORE_PHASE " + json.dumps(record, separators=(",", ":")))
                reporter.flush()
    except Exception:  # noqa: BLE001 — diagnostic failure must preserve the test's outcome.
        return


def _phase(item: pytest.Item, phase: int) -> Generator[None, Any, Any]:
    if item.path.name not in _MODULES or item.path.parent.resolve() != _DIRECTORY:
        return (yield)
    _record(item, phase, 0)
    try:
        result = yield
    except BaseException as error:
        _record(item, phase, 2, error)
        raise
    _record(item, phase, 1)
    return result


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, Any, Any]:
    return (yield from _phase(item, 0))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, Any, Any]:
    return (yield from _phase(item, 1))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, Any, Any]:
    return (yield from _phase(item, 2))
