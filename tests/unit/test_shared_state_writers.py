"""The shared-state tables replicas write whole rows into: every column but the
write's own timestamp (DEFAULT now()) is set by the writer, so a re-insert under
FINAL never lands a default over a value the previous row held."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from server.memory.store import calibration as calibration_store
from server.memory.workers import usage_fold
from server.storage import frontend_assets
from tests.unit.schema_ddl import columns


class _Capture:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}

    async def query_all(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
        self.rows.setdefault(table, []).extend(rows)


def _written_by_the_writer(table: str) -> set[str]:
    """The table's columns, less the one the server stamps for each write."""
    return {c for c, spec in columns(table).items() if "DEFAULT now()" not in spec}


def test_frontend_assets_rows_carry_every_column(tmp_path: Path) -> None:
    (tmp_path / "index-abc123.js").write_text("console.log(1)")
    ch = _Capture()
    assert asyncio.run(frontend_assets.publish(tmp_path, published_by="r1", ch=ch)) == 1
    (row,) = ch.rows["loom.frontend_assets"]
    assert set(row) >= _written_by_the_writer("frontend_assets")


def test_embedder_calibration_rows_carry_every_column() -> None:
    ch = _Capture()
    row = {
        "embedder_model": "m",
        "thresholds_json": "{}",
        "calibration_hash": "h",
        "calibrated_by": "r1",
        "version": 1,
    }
    asyncio.run(calibration_store.write_global_calibration(row, ch=ch))
    (written,) = ch.rows["loom.embedder_calibration"]
    assert set(written) == _written_by_the_writer("embedder_calibration")


def test_usage_acted_credit_rows_carry_every_column() -> None:
    ch = _Capture()
    mark = {"pair_sha256": "0" * 64, "first_write": "2026-10-14 00:00:00"}
    asyncio.run(usage_fold._record_credits(ch, [mark]))
    (written,) = ch.rows["loom.usage_acted_credits"]
    assert set(written) == _written_by_the_writer("usage_acted_credits")
