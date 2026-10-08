"""Prepared-table comparisons exercise real values, nulls and duplicate counts."""

import os
from uuid import UUID, uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.store.provider_prepared import _verify_copy
from server.insights.store.provider_staging import SourceSchema, StagingNames


@pytest.mark.asyncio
async def test_full_row_comparison_rejects_same_count_corruption():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    client = _default_client()
    await client.connect()
    names = StagingNames(uuid4(), "memory_embeddings")
    owned = {}
    schema = SourceSchema(uuid4(), "MergeTree", ("id", "text", "at", "vector"), "a" * 64)
    try:
        for table, engine, extra in (
            (names.prepared, "MergeTree ORDER BY id", ""),
            (
                names.target,
                "ReplacingMergeTree ORDER BY _loom_rebuild_ordinal",
                ", _loom_rebuild_ordinal UInt64",
            ),
        ):
            await client.execute(
                f"CREATE TABLE loom.{table} (id UInt64, text Nullable(String), "
                f"at DateTime64(6, 'UTC'), vector Array(Float32){extra}) ENGINE={engine}"
            )
            row = await client.query_one(
                "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                {"name": table},
            )
            owned[table] = UUID(str(row["uuid"]))
        await client.execute(
            f"INSERT INTO loom.{names.target} VALUES "
            "(1, NULL, '2026-10-05 12:34:56.123456', [1, 0, 0], 1), "
            "(1, NULL, '2026-10-05 12:34:56.123456', [1, 0, 0], 2), "
            "(2, 'context', '2026-10-05 12:34:56.654321', [0, 1, 0], 3)"
        )
        await client.execute(
            f"INSERT INTO loom.{names.prepared} SELECT id, text, at, vector "
            f"FROM loom.{names.target} FINAL"
        )
        await _verify_copy(names, schema, client)
        # Change a value in-place while preserving row count and all identities.
        await client.execute(
            f"ALTER TABLE loom.{names.prepared} UPDATE text='changed' WHERE id=2 "
            "SETTINGS mutations_sync=2"
        )
        with pytest.raises(ValueError, match="content differs"):
            await _verify_copy(names, schema, client)
        await client.execute(
            f"ALTER TABLE loom.{names.prepared} UPDATE text='context' WHERE id=2 "
            "SETTINGS mutations_sync=2"
        )
        await _verify_copy(names, schema, client)
        # Extra equal rows are not lost by a DISTINCT/set-style comparison.
        await client.execute(
            f"INSERT INTO loom.{names.prepared} SELECT id, text, at, vector "
            f"FROM loom.{names.target} FINAL WHERE id=1 LIMIT 1"
        )
        with pytest.raises(ValueError, match="content differs"):
            await _verify_copy(names, schema, client)
    finally:
        try:
            for table, identity in owned.items():
                current = await client.query_one(
                    "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                    {"name": table},
                )
                if current and UUID(str(current["uuid"])) == identity:
                    await client.execute(f"DROP TABLE loom.{table} SYNC")
        finally:
            await client.disconnect()
