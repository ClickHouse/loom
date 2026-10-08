"""Publish and clean an entire inventory in an independently owned database."""

import os
from uuid import UUID, uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.store.provider_cleanup import cleanup_rebuild_copies
from server.insights.store.provider_publish import publish_prepared_table
from server.insights.store.provider_staging import StagingNames


@pytest.mark.asyncio
async def test_cleanup_follows_complete_publication_and_resumes_partial_removal():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable ClickHouse instance")
    client = _default_client()
    await client.connect()
    database = f"loom_cleanup_test_{uuid4().hex}"
    job_id = uuid4()
    created = False
    try:
        await client.execute(f"CREATE DATABASE {database} ENGINE=Atomic")
        created = True
        for table in REBUILD_TABLES:
            names = StagingNames(job_id, table)
            for name, value in (
                (table, 1),
                (names.prepared, 2),
                (names.snapshot, 3),
                (names.target, 4),
            ):
                await client.execute(
                    f"CREATE TABLE {database}.{name} (value UInt8) ENGINE=MergeTree ORDER BY value"
                )
                await client.execute(f"INSERT INTO {database}.{name} VALUES ({value})")
        query = "SELECT name, uuid FROM system.tables WHERE database={database:String}"
        identities = {
            row["name"]: UUID(str(row["uuid"]))
            for row in await client.query_all(query, {"database": database})
        }
        plans = []
        for table in REBUILD_TABLES:
            names = StagingNames(job_id, table)
            plans.append(
                RebuildTablePlan(
                    table,
                    identities[table],
                    "MergeTree",
                    ("value",),
                    "b" * 64,
                    identities[names.snapshot],
                    identities[names.target],
                    identities[names.prepared],
                    1,
                )
            )
        manifest = RebuildManifest(job_id, "a" * 64, tuple(plans))
        with pytest.raises(ValueError, match="published live"):
            await cleanup_rebuild_copies(manifest, database=database, ch=client)
        assert len(await client.query_all(query, {"database": database})) == len(REBUILD_TABLES) * 4
        for table in manifest.tables:
            await publish_prepared_table(
                StagingNames(job_id, table.table),
                table.source_uuid,
                table.prepared_uuid,
                database=database,
                ch=client,
            )
        # Model a crash after the first successful private-table DROP.
        first = StagingNames(job_id, REBUILD_TABLES[0])
        await client.execute(f"DROP TABLE {database}.{first.snapshot} SYNC")
        assert (
            await cleanup_rebuild_copies(manifest, database=database, ch=client)
            == len(REBUILD_TABLES) * 3 - 1
        )
        assert await cleanup_rebuild_copies(manifest, database=database, ch=client) == 0
        remaining = await client.query_all(query, {"database": database})
        assert {row["name"]: UUID(str(row["uuid"])) for row in remaining} == {
            table.table: table.prepared_uuid for table in manifest.tables
        }
        for table in REBUILD_TABLES:
            assert await client.query_one(f"SELECT value FROM {database}.{table}") == {"value": 2}
    finally:
        try:
            if created:
                # This database was created above and contains only this test's fixtures.
                await client.execute(f"DROP DATABASE {database} SYNC")
        finally:
            await client.disconnect()
