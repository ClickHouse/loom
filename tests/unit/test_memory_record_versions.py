"""memory.record.history reads loom.memory_record_versions, the append-only
mirror of loom.memory_records.

loom.memory_records is a ReplacingMergeTree(version) keyed by record_id, and a
supersede re-inserts the prior active row under the same key. A merge keeps
only the superseded row, so a history read of the live table loses the version
that was active. The fake below applies that merge to the live table and keeps
every inserted row in the mirror, the way memory_record_versions_mv does.

The schema half pins the mirror to the table column for column (read from
schemas/schema.sql, the canonical dump), like the loom.memory_versions test.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

import server.memory.app.records as h
from server.memory.api.models_records import MemoryRecordHistoryInput
from tests.unit.schema_ddl import column_names, columns, ddl, ttl, view_select

RID = "11111111-1111-4111-8111-111111111111"
T0 = datetime(2026, 1, 1, 12, 0, 0)
T1 = datetime(2026, 1, 1, 12, 5, 0)


def _row(status: str, version: int, updated_at: datetime, valid_to: datetime | None) -> dict:
    return {
        "record_id": RID,
        "org": "o",
        "namespace": "n",
        "schema_name": "s",
        "memory_key": "k",
        "memory_id": None,
        "record_json": '{"state": "open"}',
        "status": status,
        "valid_at": None,
        "valid_to": valid_to,
        "version": version,
        "updated_at": updated_at,
        "tombstoned_at": None,
    }


class _MergingCH:
    """loom.memory_records after a background merge (one row per record,
    the highest version) next to a mirror that kept every insert."""

    def __init__(self, inserted: list[dict]) -> None:
        self.mirror = list(inserted)
        heads: dict[str, dict] = {}
        for row in inserted:
            head = heads.get(row["record_id"])
            if head is None or row["version"] > head["version"]:
                heads[row["record_id"]] = row
        self.live = list(heads.values())
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.sql.append(sql)
        p = params or {}
        if "loom.memory_record_fields" in sql:
            return []
        rows = self.mirror if "loom.memory_record_versions" in sql else self.live
        rows = [
            r
            for r in rows
            if r["org"] == p["org"]
            and r["namespace"] == p["ns"]
            and r["record_id"] in p["ids"]
            and r["tombstoned_at"] is None
        ]
        rows.sort(key=lambda r: (r["updated_at"], r["version"]), reverse=True)
        if "LIMIT 1 BY record_id, version" in sql:
            seen: set[tuple[str, int]] = set()
            rows = [
                r
                for r in rows
                if (r["record_id"], r["version"]) not in seen
                and not seen.add((r["record_id"], r["version"]))
            ]
        return [
            {
                "rid": r["record_id"],
                "memory_key": r["memory_key"],
                "mid": "",
                "record_json": r["record_json"],
                "status": r["status"],
                "valid_at": r["valid_at"],
                "valid_to": r["valid_to"],
            }
            for r in rows
        ]


def _history(ch: _MergingCH, monkeypatch) -> list:
    monkeypatch.setattr(h, "get_client", lambda: ch)
    out = asyncio.run(
        h.handle_memory_record_history(
            MemoryRecordHistoryInput(org="o", namespace="n", record_id=RID)
        )
    )
    return out.rows


def test_history_keeps_the_active_version_after_a_merge(monkeypatch) -> None:
    active = _row("active", 100, T0, None)
    superseded = _row("superseded", 200, T1, T1)
    ch = _MergingCH([active, superseded])
    assert len(ch.live) == 1  # the merge left only the superseded row

    rows = _history(ch, monkeypatch)

    # Newest first: the superseded row with its closed interval, then the
    # version that was active, still open.
    assert [r.valid_to for r in rows] == [T1, None]
    assert [r.status for r in rows] == ["superseded", "active"]
    assert all(r.record_id == RID and r.memory_key == "k" for r in rows)
    record_reads = [s for s in ch.sql if "record_json" in s]
    assert record_reads and all("FROM loom.memory_record_versions" in s for s in record_reads)


def test_history_folds_an_exact_replay_of_one_version(monkeypatch) -> None:
    # The migration's seed can overlap a write the view also mirrored.
    active = _row("active", 100, T0, None)
    ch = _MergingCH([active, dict(active)])
    assert len(_history(ch, monkeypatch)) == 1


def test_current_reads_stay_on_the_live_table(monkeypatch) -> None:
    ch = _MergingCH([_row("active", 100, T0, None)])
    monkeypatch.setattr(h, "get_client", lambda: ch)
    asyncio.run(h._records_by_ids("o", "n", [RID]))
    assert "FROM loom.memory_records FINAL" in ch.sql[0]


def test_mirror_has_every_record_column_with_the_same_type() -> None:
    assert columns("memory_record_versions") == columns("memory_records")


def test_view_copies_every_record_column() -> None:
    m = re.fullmatch(
        r"SELECT(.*?)FROM loom\.memory_records", view_select("memory_record_versions_mv"), re.S
    )
    assert m, "memory_record_versions_mv does not select from loom.memory_records"
    selected = [tok.strip().strip("`") for tok in m.group(1).split(",")]
    assert selected == column_names("memory_records")


def test_mirror_sort_key_ends_in_version_so_distinct_writes_never_collapse() -> None:
    m = re.search(r"ORDER BY \((.*?)\)", ddl("memory_record_versions"))
    assert m and m.group(1).split(",")[-1].strip() == "version"
    assert ttl("memory_record_versions") is None
