"""The JSON log line's `ts` is the record's instant as UTC ISO-8601 with microseconds."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import pytest

from server.core.logging_config import _JsonFormatter


def _ts_of(created: float) -> str:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", (), None)
    record.created = created
    return str(json.loads(_JsonFormatter().format(record))["ts"])


def test_ts_carries_microseconds_in_utc() -> None:
    created = datetime(2026, 3, 1, 12, 34, 56, 789012, tzinfo=UTC).timestamp()
    assert _ts_of(created) == "2026-03-01T12:34:56.789012Z"


def test_ts_ignores_the_process_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        created = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC).timestamp()
        assert _ts_of(created) == "2026-03-01T12:00:00.000000Z"
    finally:
        monkeypatch.undo()
        time.tzset()
