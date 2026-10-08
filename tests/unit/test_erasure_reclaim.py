"""The erasure reclaim worker removes the bytes of purged rows, one table per pass.

A purge of a wide table only masks its rows; the worker gives the purge table
with the most dead bytes past both thresholds one `APPLY DELETED MASK`, and
leaves a table alone while any mutation runs on it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.erasure.workers import reclaim
from server.erasure.workers.reclaim import TableDead, pick

GiB = 1 << 30


def test_dead_share_and_bytes() -> None:
    t = TableDead("loom.memory_chunks", on_disk=4_889_896, visible=2_094_831, bytes=31 * GiB)
    assert round(t.dead_fraction, 3) == 0.572 and t.dead_bytes > 17 * GiB
    assert TableDead("loom.x", on_disk=0, visible=0, bytes=0).dead_fraction == 0.0


def test_pick_takes_the_most_dead_bytes_past_both_thresholds() -> None:
    small_share = TableDead("loom.a", on_disk=100, visible=90, bytes=50 * GiB)  # 10% dead
    few_bytes = TableDead("loom.b", on_disk=100, visible=10, bytes=GiB // 2)  # 0.45 GiB dead
    big = TableDead("loom.c", on_disk=100, visible=40, bytes=10 * GiB)  # 6 GiB dead
    bigger = TableDead("loom.d", on_disk=100, visible=50, bytes=20 * GiB)  # 10 GiB dead
    kw = {"min_fraction": 0.3, "min_bytes": GiB}
    assert pick([small_share, few_bytes, big, bigger], **kw) == (bigger, 2)
    assert pick([small_share, few_bytes], **kw) == (None, 0)


class _CH:
    def __init__(self, tables: dict[str, dict[str, int]]) -> None:
        self.tables = tables
        self.executed: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if "FROM system.parts" in sql:
            t = self.tables.get(f"loom.{(params or {})['t']}", {})
            return {k: t.get(k, 0) for k in ("on_disk", "bytes", "masked_parts")}
        if "FROM system.mutations" in sql:
            return {"n": self.tables.get(f"loom.{(params or {})['t']}", {}).get("pending", 0)}
        table = sql.split("FROM ", 1)[1].strip()
        return {"n": self.tables.get(table, {}).get("visible", 0)}

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        self.executed.append(sql)


def _run(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> reclaim.ReclaimResult:
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    return asyncio.run(reclaim.run_pass())


def test_a_pass_masks_the_worst_table_and_skips_one_with_a_running_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead = {"on_disk": 100, "visible": 40, "masked_parts": 3}
    ch = _CH(
        {
            "loom.memory_chunks": {**dead, "bytes": 30 * GiB, "pending": 1},  # busy: skipped
            "loom.entity_vectors": {**dead, "bytes": 10 * GiB},
            "loom.memory_evidence": {**dead, "bytes": 2 * GiB},
        }
    )
    out = _run(ch, monkeypatch)
    assert out.reclaimed == "loom.entity_vectors" and out.candidates == 2
    assert ch.executed == [
        "ALTER TABLE loom.entity_vectors APPLY DELETED MASK SETTINGS mutations_sync = 0"
    ]


def test_a_pass_with_nothing_masked_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH({"loom.memory_chunks": {"on_disk": 100, "visible": 100, "bytes": 30 * GiB}})
    out = _run(ch, monkeypatch)
    assert out.reclaimed == "" and out.candidates == 0 and ch.executed == []


def test_the_thresholds_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    assert settings.erasure_reclaim_enabled is True
    assert settings.erasure_reclaim_interval_seconds == 3600
    dead = {"on_disk": 100, "visible": 40, "masked_parts": 3, "bytes": 2 * GiB}  # 1.2 GiB dead
    monkeypatch.setattr(settings, "erasure_reclaim_min_dead_bytes", 2 * GiB)
    assert _run(_CH({"loom.memory_evidence": dict(dead)}), monkeypatch).reclaimed == ""
    monkeypatch.setattr(settings, "erasure_reclaim_min_dead_bytes", GiB)
    monkeypatch.setattr(settings, "erasure_reclaim_min_dead_fraction", 0.7)
    assert _run(_CH({"loom.memory_evidence": dict(dead)}), monkeypatch).reclaimed == ""
    monkeypatch.setattr(settings, "erasure_reclaim_min_dead_fraction", 0.5)
    assert (
        _run(_CH({"loom.memory_evidence": dict(dead)}), monkeypatch).reclaimed
        == "loom.memory_evidence"
    )


def test_the_registry_runs_erasure_reclaim_hourly() -> None:
    from server.workers.registry import enabled_workers

    [spec] = [w for w in enabled_workers() if w.name == "erasure_reclaim"]
    assert spec.interval_seconds == 3600
