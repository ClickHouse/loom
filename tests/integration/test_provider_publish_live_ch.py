"""Exercise forward-only publication in an independently owned test database."""

import os
from uuid import UUID, uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.store.provider_publish import publish_prepared_table
from server.insights.store.provider_staging import StagingNames


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted_at", ["before", "between", "after"])
async def test_publication_resumes_each_persisted_state(interrupted_at):
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable ClickHouse instance")
    client = _default_client()
    await client.connect()
    database = f"loom_publish_test_{uuid4().hex}"
    names = StagingNames(uuid4(), "memory_embeddings")
    created = False
    try:
        await client.execute(f"CREATE DATABASE {database} ENGINE = Atomic")
        created = True
        for table, value in ((names.table, 1), (names.prepared, 2)):
            await client.execute(
                f"CREATE TABLE {database}.{table} (value UInt8) ENGINE = MergeTree ORDER BY value"
            )
            await client.execute(f"INSERT INTO {database}.{table} VALUES ({value})")
        tables = await client.query_all(
            "SELECT name, uuid FROM system.tables WHERE database = {database:String}",
            {"database": database},
        )
        identities = {row["name"]: UUID(str(row["uuid"])) for row in tables}
        old, new = identities[names.table], identities[names.prepared]
        if interrupted_at in ("between", "after"):
            await client.execute(
                f"RENAME TABLE {database}.{names.table} TO {database}.{names.backup}"
            )
        if interrupted_at == "after":
            await client.execute(
                f"RENAME TABLE {database}.{names.prepared} TO {database}.{names.table}"
            )
        for _ in range(2):
            await publish_prepared_table(names, old, new, database=database, ch=client)
            assert await client.query_one(f"SELECT value FROM {database}.{names.table}") == {
                "value": 2
            }
            assert await client.query_one(f"SELECT value FROM {database}.{names.backup}") == {
                "value": 1
            }
        final = await client.query_all(
            "SELECT name, uuid FROM system.tables WHERE database = {database:String}",
            {"database": database},
        )
        assert {row["name"]: UUID(str(row["uuid"])) for row in final} == {
            names.table: new,
            names.backup: old,
        }
    finally:
        if created:
            # This unique database was created above and contains only these fixtures.
            await client.execute(f"DROP DATABASE {database} SYNC")
        await client.disconnect()
