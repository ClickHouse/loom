"""Task event cursors across replicas: `seq` and `ts` come from the ClickHouse
server's clock, the cursor read is sequentially consistent and stays a lag
behind the server's now, so an event another replica stamped (with a slower
or faster host clock) is never below a cursor that already passed it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.tasks.domain import cursors
from server.tasks.store import repo
from tests.unit.test_task_inbox import _post, _register, fake  # noqa: F401 — fixture

SERVER_NOW_US = 1_800_000_000_000_000  # the server's clock, far from any host's


class _RecordingCH:
    def __init__(self, v: int = SERVER_NOW_US) -> None:
        self.v = v
        self.sql: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sql.append((sql, params or {}))
        return {"v": self.v}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.sql.append((sql, params or {}))
        return []


@pytest.mark.asyncio
async def test_server_version_reads_the_server_clock_and_beats_the_row() -> None:
    ch = _RecordingCH()
    v = await repo.server_version(0, ch=ch)  # type: ignore[arg-type]
    assert v >= SERVER_NOW_US
    assert "now64(6)" in ch.sql[0][0]
    # A row an older build stamped ahead of the server keeps losing to nothing.
    ahead = SERVER_NOW_US + 10**9
    assert await repo.server_version(ahead, ch=ch) == ahead + 1  # type: ignore[arg-type]
    # Strictly increasing inside the process.
    assert await repo.server_version(0, ch=ch) > ahead + 1  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_cursor_read_is_sequential_and_lags_the_server_clock() -> None:
    ch = _RecordingCH()
    await repo.events_since("acme", since_seq=5, lag_s=cursors.LAG_SECONDS, inbox="bot", ch=ch)  # type: ignore[arg-type]
    sql, params = ch.sql[-1]
    assert "select_sequential_consistency = 1" in sql
    assert "ts <= now64(3) - toIntervalSecond({lag:Int32})" in sql
    assert params["lag"] == cursors.LAG_SECONDS and params["since"] == 5


@pytest.mark.asyncio
async def test_a_transition_stamps_seq_and_ts_from_the_server(
    fake: Any,  # noqa: F811 — the fixture
) -> None:
    async def server_version(floor: int = 0) -> int:
        return max(SERVER_NOW_US, floor + 1)

    fake.server_version = server_version
    await _register(fake)
    out = await _post(fake)
    ev = fake.events[-1]
    assert out.seq == ev["seq"] == ev["version"] == SERVER_NOW_US
    assert ev["ts"] == datetime.fromtimestamp(SERVER_NOW_US / 1e6, UTC).replace(tzinfo=None)
    assert out.task.version == SERVER_NOW_US
