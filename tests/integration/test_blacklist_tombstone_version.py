"""A refusal tombstone must dominate the selected row's stored version."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from server.core.ch import get_client, set_query_tag
from server.memory.store.blacklist import insert_row, tombstone_pattern
from tests.integration._resources import unique_org


@pytest.fixture
async def pattern_store() -> AsyncIterator[Any]:
    client = get_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


def _row(org: str, pattern_id: str, created_at: datetime) -> dict[str, Any]:
    return {
        "pattern_id": pattern_id,
        "org": org,
        "namespace": "version-proof",
        "pattern": "refusal-" + pattern_id,
        "pattern_type": "substring",
        "scope": "namespace",
        "reason": "retain the selected fields",
        "created_by": "process:version-proof",
        "hit_count": 7,
        "last_hit_at": created_at - timedelta(seconds=1),
        "created_at": created_at,
        "tombstoned_at": None,
    }


async def _stored(client: Any, row: dict[str, Any]) -> dict[str, Any]:
    result = await client.query_one(
        "SELECT pattern_id, org, namespace, pattern, pattern_type, scope, reason, created_by, "
        "hit_count, created_at, tombstoned_at, last_hit_at FROM loom.blacklist FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND pattern_id = {pid:UUID}",
        params={"org": row["org"], "ns": row["namespace"], "pid": row["pattern_id"]},
    )
    assert result is not None
    return dict(result)


async def _remove_fixture(client: Any, row: dict[str, Any]) -> None:
    await client.execute(
        "DELETE FROM loom.blacklist WHERE org = {org:String} AND namespace = {ns:String} "
        "AND pattern_id = {pid:UUID} SETTINGS lightweight_deletes_sync = 2",
        params={"org": row["org"], "ns": row["namespace"], "pid": row["pattern_id"]},
    )


def _comparable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.replace(tzinfo=None)
        if isinstance(value, datetime)
        else str(value)
        if key == "pattern_id"
        else value
        for key, value in row.items()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("ahead_seconds", [0, 86400], ids=["current", "future"])
async def test_tombstone_dominates_version_and_preserves_selected_fields(
    pattern_store: Any,
    ahead_seconds: int,
) -> None:
    set_query_tag("test.blacklist.tombstone_version")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    row = _row(
        unique_org("blacklist-version"), str(uuid4()), now + timedelta(seconds=ahead_seconds)
    )
    try:
        await insert_row(row, ch=pattern_store)
        before = await _stored(pattern_store, row)
        assert _comparable(before) == row
        assert before["tombstoned_at"] is None
        await tombstone_pattern(row["pattern_id"], now, ch=pattern_store)
        after = await _stored(pattern_store, row)
        assert after["created_at"].replace(tzinfo=None) > before["created_at"].replace(tzinfo=None)
        assert after["tombstoned_at"].replace(tzinfo=None) == now
        assert {
            key: value for key, value in after.items() if key not in {"created_at", "tombstoned_at"}
        } == {
            key: value
            for key, value in before.items()
            if key not in {"created_at", "tombstoned_at"}
        }
    finally:
        await _remove_fixture(pattern_store, row)


@pytest.mark.asyncio
async def test_tombstone_refuses_unrepresentable_successor_without_changing_row(
    pattern_store: Any,
) -> None:
    set_query_tag("test.blacklist.tombstone_overflow")
    maximum = datetime(1970, 1, 1) + timedelta(seconds=2**32 - 1)
    row = _row(unique_org("blacklist-version"), str(uuid4()), maximum)
    try:
        await insert_row(row, ch=pattern_store)
        before = await _stored(pattern_store, row)
        assert _comparable(before) == row
        with pytest.raises(ValueError):
            await tombstone_pattern(row["pattern_id"], datetime.now(UTC), ch=pattern_store)
        assert await _stored(pattern_store, row) == before
    finally:
        await _remove_fixture(pattern_store, row)
