"""A failed merge is a health failure.

When every merge on a table fails (for example on a skip index the server
cannot build), parts pile to the insert limit and every INSERT is refused,
while a part count alone only says "warn". The check reads system.part_log
for failed merges and fails
on them, or on a table within 10 % of parts_to_throw_insert, naming the
repair (exclude_materialize_skip_indexes_on_merge).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.insights.api.models_ops import OpsHealthInput
from server.insights.app import ops as ops_mod


class _CH:
    """Answers `query_all` / `query_one` by a marker in the SQL; unknown SQL
    is an empty answer, so the other health checks stay quiet."""

    def __init__(
        self,
        parts: list[dict[str, Any]],
        failed: list[dict[str, Any]] | Exception,
    ):
        self.parts = parts
        self.failed = failed
        self.sqls: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        if "FROM system.part_log" in sql:
            if isinstance(self.failed, Exception):
                raise self.failed
            return list(self.failed)
        if "FROM system.parts" in sql:
            return list(self.parts)
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        return None


def _parts_check(monkeypatch: pytest.MonkeyPatch, ch: _CH) -> Any:
    import server.core.ch as _ch

    monkeypatch.setattr(_ch, "get_client", lambda: ch)
    out = asyncio.run(ops_mod.handle_ops_health(OpsHealthInput(org="o")))
    return next(c for c in out.checks if c.name == "clickhouse_parts")


_FAILURE = {
    "table": "memory_embeddings",
    "failures": 33203,
    "exception": (
        "Code: 49. DB::Exception: Text index transform for index content_excerpt_text "
        "and part 202609_395844_395889_2 not found. (LOGICAL_ERROR)"
    ),
    "merge_reason": "TTLDropMerge",
    "last_failure": "2026-09-20 09:43:12",
}


def test_failed_merges_fail_the_check_and_name_the_exception_and_the_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH(
        parts=[{"table": "memory_embeddings", "parts": 3001, "rows": 2_750_023}], failed=[_FAILURE]
    )
    c = _parts_check(monkeypatch, ch)
    assert c.status == "fail"
    assert "33203 failed merges on loom.memory_embeddings" in c.detail
    assert "content_excerpt_text" in c.detail
    assert "exclude_materialize_skip_indexes_on_merge" in c.repair
    assert c.data["failed_merges"] == [_FAILURE]
    # The part_log read is bounded to the last quarter hour and to merges.
    sql = next(q for q in ch.sqls if "system.part_log" in q)
    assert (
        "INTERVAL 15 MINUTE" in sql and "event_type = 'MergeParts'" in sql and "error != 0" in sql
    )
    assert "merge_reason" in sql


def test_a_table_near_the_insert_limit_fails_even_when_merges_are_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _parts_check(
        monkeypatch,
        _CH(parts=[{"table": "memory_embeddings", "parts": 2700, "rows": 1}], failed=[]),
    )
    assert c.status == "fail"
    assert "2700 active parts" in c.detail and "3000" in c.detail
    assert ops_mod.PARTS_FAIL_AT == 2700


def test_half_way_to_the_limit_without_failures_is_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _parts_check(
        monkeypatch, _CH(parts=[{"table": "otel_traces", "parts": 1600, "rows": 1}], failed=[])
    )
    assert c.status == "warn"
    assert "max active parts per loom table = 1600" in c.detail
    assert "batching" in c.repair and "part_log" in c.repair
    assert ops_mod.PARTS_WARN_AT == 1500
    # Below half-way is ordinary.
    c = _parts_check(
        monkeypatch, _CH(parts=[{"table": "otel_traces", "parts": 1200, "rows": 1}], failed=[])
    )
    assert c.status == "ok"


def test_a_quiet_store_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _parts_check(
        monkeypatch, _CH(parts=[{"table": "memory", "parts": 25, "rows": 1}], failed=[])
    )
    assert c.status == "ok" and c.repair == "" and c.data["failed_merges"] == []


def test_a_fresh_server_without_part_log_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # system.part_log appears after the first flush; UNKNOWN_TABLE (60) until then.
    absent = RuntimeError(
        "Code: 60. DB::Exception: Unknown table expression identifier 'system.part_log'"
    )
    c = _parts_check(
        monkeypatch, _CH(parts=[{"table": "memory", "parts": 25, "rows": 1}], failed=absent)
    )
    assert c.status == "ok" and c.data["failed_merges"] == []
    assert str(c.data["part_log"]).startswith("absent: Code: 60")
    # …and the parts thresholds still apply without it.
    c = _parts_check(
        monkeypatch, _CH(parts=[{"table": "memory", "parts": 2800, "rows": 1}], failed=absent)
    )
    assert c.status == "fail" and "2800 active parts" in c.detail
