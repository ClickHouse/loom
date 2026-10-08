"""Snapshot-phase recovery reconciles receipts before excluding private tables."""

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES, REBUILD_TABLES
from server.insights.store import provider_snapshot_plan as plan
from server.insights.store.provider_staging import SourceSchema, StagingNames, completion_marker
from server.insights.store.provider_target import target_marker


@pytest.fixture
def copies():
    job = uuid4()
    schema = SourceSchema(uuid4(), "ReplacingMergeTree", ("embedding",), "a" * 64)
    names = StagingNames(job, "memory_embeddings")
    snapshot = {
        "name": names.snapshot,
        "uuid": uuid4(),
        "engine": "MergeTree",
        "comment": completion_marker(names, schema),
    }
    target = {
        "name": names.target,
        "uuid": uuid4(),
        "engine": "ReplacingMergeTree",
        "comment": target_marker(names, snapshot["uuid"], "b" * 64),
    }
    return job, names, schema, snapshot, target


def test_complete_receipts_recognize_both_owned_copies(copies):
    job, names, schema, snapshot, target = copies
    assert plan._recovery_copies(job, "b" * 64, {names.table: schema}, [snapshot, target]) == {
        names.snapshot: snapshot["uuid"],
        names.target: target["uuid"],
    }


def test_incomplete_source_is_accounted_for_but_cannot_have_a_target(copies):
    job, names, schema, snapshot, target = copies
    snapshot["comment"] = names.marker(schema.table_uuid)
    assert plan._recovery_copies(job, "b" * 64, {names.table: schema}, [snapshot]) == {
        names.snapshot: snapshot["uuid"]
    }
    with pytest.raises(ValueError, match="unfinished source"):
        plan._recovery_copies(job, "b" * 64, {names.table: schema}, [snapshot, target])


@pytest.mark.parametrize(
    "damage",
    [
        "source_uuid",
        "schema",
        "source_engine",
        "target_owner",
        "target_engine",
        "missing_source",
        "unexpected",
        "duplicate",
    ],
)
def test_unproven_or_unexpected_copies_cannot_be_excluded(copies, damage):
    job, names, schema, snapshot, target = copies
    rows = [snapshot, target]
    if damage == "source_uuid":
        snapshot["uuid"] = UUID(int=0)
    elif damage == "schema":
        snapshot["comment"] = snapshot["comment"][:-64] + "c" * 64
    elif damage == "source_engine":
        snapshot["engine"] = "Memory"
    elif damage == "target_owner":
        target["comment"] = target_marker(names, uuid4(), "b" * 64)
    elif damage == "target_engine":
        target["engine"] = "MergeTree"
    elif damage == "missing_source":
        rows = [target]
    elif damage == "unexpected":
        rows.append({**target, "name": names.prepared})
    else:
        rows.append(snapshot)
    with pytest.raises(ValueError):
        plan._recovery_copies(job, "b" * 64, {names.table: schema}, rows)


@pytest.mark.asyncio
async def test_recovery_preflight_checks_all_live_surfaces_without_writes(monkeypatch):
    schemas = {
        table: SourceSchema(uuid4(), "MergeTree", ("embedding",), "a" * 64)
        for table in REBUILD_TABLES
    }
    monkeypatch.setattr(
        plan, "source_schema", AsyncMock(side_effect=lambda names, **_: schemas[names.table])
    )
    columns = [
        {"table": surface.table, "name": surface.column, "type": "Array(Float32)"}
        for surface in EMBEDDING_SURFACES
    ]
    client = AsyncMock()
    client.query_all.side_effect = [[], columns]
    assert await plan.preflight_snapshot_recovery(uuid4(), "b" * 64, ch=client) == schemas
    client.execute.assert_not_awaited()
    client.insert.assert_not_awaited()
    client.query_all.side_effect = [
        [],
        columns + [{"table": "unrecognized", "name": "vector", "type": "Array(Float32)"}],
    ]
    with pytest.raises(ValueError, match="unknown="):
        await plan.preflight_snapshot_recovery(uuid4(), "b" * 64, ch=client)
    client.execute.assert_not_awaited()
