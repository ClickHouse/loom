"""A test or gate on a live ClickHouse flushes the system logs it reads, by name.

A bare `SYSTEM FLUSH LOGS` waits for every system log. On a loaded runner the
flush of a log the caller never reads (trace_log) can outlast the server's
180 s bound, and the statement answers error 159 before any assertion runs.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_ON_LIVE_CLICKHOUSE = ("tests/integration", "scripts/gates")
_BARE_FLUSH = re.compile(r"SYSTEM\s+FLUSH\s+LOGS(?!\s+\w)", re.IGNORECASE)


def test_no_flush_waits_for_every_system_log() -> None:
    bare = [
        f"{path.relative_to(ROOT)}:{number}"
        for folder in _ON_LIVE_CLICKHOUSE
        for path in sorted((ROOT / folder).rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _BARE_FLUSH.search(line)
    ]
    assert not bare, f"name the logs read, as in `SYSTEM FLUSH LOGS query_log`: {bare}"
