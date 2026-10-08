"""The snapshot checkpoint contains only completed source and owned target UUIDs."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.app import provider_rebuild_snapshot as snapshot
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.store.provider_staging import SourceSchema


@pytest.fixture
def backend(monkeypatch):
    schemas = {
        table: SourceSchema(uuid4(), "MergeTree", ("embedding",), "a" * 64)
        for table in REBUILD_TABLES
    }
    source_ids = {table: uuid4() for table in REBUILD_TABLES}
    target_ids = {table: uuid4() for table in REBUILD_TABLES}
    preflight = AsyncMock(return_value=schemas)
    sources = AsyncMock(side_effect=lambda names, *args, **kwargs: source_ids[names.table])
    targets = AsyncMock(side_effect=lambda names, *args, **kwargs: target_ids[names.table])
    monkeypatch.setattr(snapshot, "preflight_snapshot_recovery", preflight)
    monkeypatch.setattr(snapshot, "ensure_snapshot", sources)
    monkeypatch.setattr(snapshot, "ensure_target", targets)
    return schemas, source_ids, target_ids, preflight, sources, targets


@pytest.mark.asyncio
async def test_manifest_can_be_recovered_with_identical_receipts(backend):
    schemas, source_ids, target_ids, preflight, sources, targets = backend
    job = uuid4()
    first = await snapshot.snapshot_rebuild_manifest(job, "b" * 64, prefix_dimensions=(256,))
    second = await snapshot.snapshot_rebuild_manifest(job, "b" * 64, prefix_dimensions=(256,))
    assert first == second
    assert first.prefix_dimensions == (256,)
    assert tuple(table.table for table in first.tables) == REBUILD_TABLES
    for table in first.tables:
        assert table.source_uuid == schemas[table.table].table_uuid
        assert table.snapshot_uuid == source_ids[table.table]
        assert table.target_uuid == target_ids[table.table]
        assert table.prepared_uuid is None
    assert preflight.await_count == 2
    assert sources.await_count == targets.await_count == len(REBUILD_TABLES) * 2


@pytest.mark.asyncio
async def test_inventory_failure_prevents_every_copy(backend):
    *_, preflight, sources, targets = backend
    preflight.side_effect = ValueError("unknown vector")
    with pytest.raises(ValueError, match="unknown vector"):
        await snapshot.snapshot_rebuild_manifest(uuid4(), "b" * 64, prefix_dimensions=())
    sources.assert_not_awaited()
    targets.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_snapshot_never_creates_a_target(backend):
    *_, sources, targets = backend
    sources.side_effect = RuntimeError("snapshot response lost")
    with pytest.raises(RuntimeError, match="response lost"):
        await snapshot.snapshot_rebuild_manifest(uuid4(), "b" * 64, prefix_dimensions=())
    targets.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_policy_is_rejected_before_preflight(backend):
    *_, preflight, sources, targets = backend
    with pytest.raises(ValueError, match="prefix policy"):
        await snapshot.snapshot_rebuild_manifest(uuid4(), "b" * 64, prefix_dimensions=(True,))
    preflight.assert_not_awaited()
    sources.assert_not_awaited()
    targets.assert_not_awaited()
