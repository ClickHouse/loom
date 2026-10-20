"""memory.keys' statement on clickhouse-local against the real loom.memory table: every live
row's identity and key, superseded and duplicate rows included, deleted and proposed rows
left out, the way memory.list serves them to export-okf."""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


class _LocalCH:
    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)


def _row(key: str, version: int, *, status: str = "active", deleted: bool = False) -> str:
    tombstoned = "now()" if deleted else "NULL"
    return (
        "INSERT INTO loom.memory (org, namespace, agent, memory_key, version, status, "
        f"tombstoned_at) VALUES ('o', 'n', 'a', '{key}', {version}, '{status}', {tombstoned})"
    )


@pytest.mark.asyncio
async def test_live_keys_reads_what_the_export_lists() -> None:
    ch = _LocalCH(
        ddl("memory"),
        _row("live", 1),
        _row("old", 1, status="superseded"),
        _row("copy", 1, status="duplicate"),
        _row("trial", 1, status="proposed"),
        _row("gone", 1),
        _row("gone", 2, deleted=True),
    )
    rows = await memory_repo.live_keys("o", "n", scope=WHOLE_NAMESPACE, limit=10, ch=ch)  # type: ignore[arg-type]
    assert sorted(r["memory_key"] for r in rows) == ["copy", "live", "old"]


@pytest.mark.asyncio
async def test_live_keys_reads_one_more_than_the_limit() -> None:
    ch = _LocalCH(ddl("memory"), *(_row(f"k{i}", 1) for i in range(3)))
    rows = await memory_repo.live_keys("o", "n", scope=WHOLE_NAMESPACE, limit=2, ch=ch)  # type: ignore[arg-type]
    assert len(rows) == 3
