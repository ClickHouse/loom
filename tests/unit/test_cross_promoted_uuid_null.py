"""cross_promoted_from_memory_id is Nullable(UUID): empty means NULL.

ClickHouse parses '' into the zero UUID, not NULL, so a column carried
forward as '' would turn every re-emitted row into "promoted from
00000000-…". The builder, the hydrator and both copy paths funnel the value
through uuid_or_none.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server.memory import store as memory_repo
from server.memory.store import (
    ZERO_UUID,
    MemoryRow,
    build_memory_version_row,
    uuid_or_none,
)

_REAL = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def _row(**over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": "v",
        "confidence": 1.0,
        "version": 1,
        "pinned": False,
        "created_at": datetime(2026, 9, 12, tzinfo=UTC),
        "expires_at": None,
    }
    base.update(over)
    return MemoryRow(**base)


def test_uuid_or_none_maps_missing_empty_and_zero_to_none() -> None:
    assert uuid_or_none(None) is None
    assert uuid_or_none("") is None
    assert uuid_or_none("   ") is None
    assert uuid_or_none(ZERO_UUID) is None
    assert uuid_or_none(_REAL) == _REAL


def test_version_row_lands_null_not_empty_string() -> None:
    # Default MemoryRow (never promoted).
    row = build_memory_version_row(_row(), recall_count=3)
    assert row["cross_promoted_from_memory_id"] is None
    # A row hydrated with the zero UUID is repaired on its next write.
    row = build_memory_version_row(_row(cross_promoted_from_memory_id=ZERO_UUID))
    assert row["cross_promoted_from_memory_id"] is None
    # A real promotion pointer survives every re-emit.
    row = build_memory_version_row(_row(cross_promoted_from_memory_id=_REAL))
    assert row["cross_promoted_from_memory_id"] == _REAL
    # The namespace half stays a plain non-nullable String.
    assert row["cross_promoted_from_namespace"] == ""


def test_hydrate_reads_zero_uuid_as_not_promoted() -> None:
    base = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": "v",
        "confidence": 1.0,
        "version": 1,
        "pinned": 0,
        "created_at": datetime(2026, 9, 12, tzinfo=UTC),
        "expires_at": None,
    }
    assert (
        memory_repo._hydrate(
            {**base, "cross_promoted_from_memory_id": ZERO_UUID}
        ).cross_promoted_from_memory_id
        == ""
    )
    assert (
        memory_repo._hydrate(
            {**base, "cross_promoted_from_memory_id": None}
        ).cross_promoted_from_memory_id
        == ""
    )
    assert (
        memory_repo._hydrate(
            {**base, "cross_promoted_from_memory_id": _REAL}
        ).cross_promoted_from_memory_id
        == _REAL
    )
