"""A provider change must account for all persisted vector spaces, not only memory."""

import re
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.domain.provider_rebuild import (
    EMBEDDING_SURFACES,
    REBUILD_TABLES,
    rebuild_surfaces,
)
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.store.provider_rebuild import (
    rebuild_inventory,
    tables_with_vectors,
    unpublishable_rebuild_tables,
)
from server.insights.store.provider_staging import StagingNames


def schema_columns() -> list[dict[str, str]]:
    schema = (Path(__file__).resolve().parents[2] / "schemas/schema.sql").read_text()
    columns = []
    for table, body in re.findall(
        r"CREATE TABLE IF NOT EXISTS loom\.(\w+)\s*\((.*?)\n\)\nENGINE", schema, re.S
    ):
        for name, column_type in re.findall(r"^\s+`([^`]+)` (.+)$", body, re.M):
            columns.append({"table": table, "name": name, "type": column_type})
    return columns


def test_every_canonical_vector_column_has_an_explicit_rebuild_action():
    assert rebuild_surfaces(schema_columns()) == EMBEDDING_SURFACES
    keys = [(surface.table, surface.column) for surface in EMBEDDING_SURFACES]
    assert len(keys) == len(set(keys))
    assert ("memory_embeddings", "memory_key_embedding") in keys


@pytest.mark.parametrize("column_type", ["Array(Float32)", "QBit(Float32, 3072)"])
@pytest.mark.parametrize("column_name", ["embedding", "vector"])
def test_unregistered_operator_vector_table_fails_closed(column_type, column_name):
    columns = schema_columns() + [
        {"table": "custom_vectors", "name": column_name, "type": column_type}
    ]
    with pytest.raises(ValueError, match=rf"unknown=\[custom_vectors\.{column_name}\]"):
        rebuild_surfaces(columns)


def test_partial_database_schema_cannot_look_like_a_complete_rebuild():
    columns = [row for row in schema_columns() if row["table"] != "qa_item_embeddings"]
    with pytest.raises(ValueError, match=r"missing=\[qa_item_embeddings.embedding\]"):
        rebuild_surfaces(columns)


def test_historical_evidence_and_disposable_cache_are_not_reembedded():
    actions = {surface.table: surface.action for surface in EMBEDDING_SURFACES}
    assert actions["events"] == "preserve_history"
    assert actions["embedding_cache"] == "invalidate_cache"
    assert actions["code_chunks_prefix"] == "derive_prefix"


@pytest.mark.asyncio
async def test_live_preflight_reads_metadata_without_changing_data():
    client = AsyncMock()
    client.query_all.return_value = schema_columns()
    assert await rebuild_inventory(ch=client) == EMBEDDING_SURFACES
    client.query_all.assert_awaited_once()
    client.insert.assert_not_awaited()
    client.command.assert_not_awaited()


@pytest.fixture
def manifest():
    return RebuildManifest(
        uuid4(),
        "a" * 64,
        tuple(
            RebuildTablePlan(
                table,
                uuid4(),
                "MergeTree",
                ("embedding",),
                "b" * 64,
                uuid4(),
                uuid4(),
                uuid4(),
                0,
            )
            for table in REBUILD_TABLES
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["snapshot", "target", "prepared", "backup"])
async def test_recovery_excludes_only_manifest_owned_uuid(manifest, kind):
    table = manifest.tables[0]
    names = StagingNames(manifest.job_id, table.table)
    identity = {
        "snapshot": table.snapshot_uuid,
        "target": table.target_uuid,
        "prepared": table.prepared_uuid,
        "backup": table.source_uuid,
    }[kind]
    client = AsyncMock()
    extra = {
        "table": getattr(names, kind),
        "name": "embedding",
        "type": "Array(Float32)",
        "table_uuid": identity,
    }
    client.query_all.return_value = [*schema_columns(), extra]
    assert await rebuild_inventory(manifest=manifest, ch=client) == EMBEDDING_SURFACES
    extra["table_uuid"] = uuid4()
    with pytest.raises(ValueError, match="replaced staging"):
        await rebuild_inventory(manifest=manifest, ch=client)
    client.execute.assert_not_awaited()
    client.insert.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_job", [False, True])
async def test_staging_prefix_alone_never_hides_an_unknown_vector(manifest, other_job):
    names = StagingNames(uuid4() if other_job else manifest.job_id, manifest.tables[0].table)
    client = AsyncMock()
    client.query_all.return_value = [
        *schema_columns(),
        {
            "table": names.snapshot,
            "name": "embedding",
            "type": "Array(Float32)",
            "table_uuid": manifest.tables[0].snapshot_uuid,
        },
    ]
    with pytest.raises(ValueError, match="unknown="):
        await rebuild_inventory(manifest=manifest if other_job else None, ch=client)


@pytest.mark.asyncio
async def test_unpublishable_tables_are_rebuild_sources_on_other_engines():
    client = AsyncMock()
    client.query_all.return_value = [
        {"name": "code_chunks", "engine": "SharedReplacingMergeTree"},
        {"name": "memory_embeddings", "engine": "ReplacingMergeTree"},
        {"name": "query_nodes", "engine": "ReplicatedMergeTree"},
    ]
    assert await unpublishable_rebuild_tables(ch=client) == (
        ("code_chunks", "SharedReplacingMergeTree"),
        ("query_nodes", "ReplicatedMergeTree"),
    )
    assert client.query_all.await_args.args[1] == {"tables": list(REBUILD_TABLES)}


@pytest.mark.asyncio
async def test_tables_with_vectors_counts_unknown_row_counts_as_rows():
    client = AsyncMock()
    client.query_all.return_value = [
        {"name": "chunks", "total_rows": 0},
        {"name": "code_chunks", "total_rows": None},
        {"name": "memory_embeddings", "total_rows": 12},
    ]
    assert await tables_with_vectors(ch=client) == ("code_chunks", "memory_embeddings")
    assert "embedding_cache" not in client.query_all.await_args.args[1]["tables"]
