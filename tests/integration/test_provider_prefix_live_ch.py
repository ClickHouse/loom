"""Real prefix arithmetic over private fixtures, without changing live code rows."""

import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.app.provider_rebuild_page import rebuild_snapshot_page
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.store.provider_prefix import derive_staged_prefixes, verify_staged_prefixes
from server.insights.store.provider_staging import StagingNames, ensure_snapshot, source_schema
from server.insights.store.provider_target import ensure_target


@pytest.mark.asyncio
async def test_prefix_derivation_and_corruption_detection_on_clickhouse():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    client = _default_client()
    await client.connect()
    job = uuid4()
    owned = {}
    plans = {}
    try:
        for table in ("code_chunks", "code_chunks_prefix"):
            names = StagingNames(job, table)
            schema = await source_schema(names, ch=client)
            snapshot = await ensure_snapshot(names, schema, ch=client)
            owned[names.snapshot] = snapshot
            target = await ensure_target(names, schema, snapshot, "a" * 64, ch=client)
            owned[names.target] = target
            plans[table] = RebuildTablePlan(
                table,
                schema.table_uuid,
                schema.engine,
                schema.columns,
                schema.fingerprint,
                snapshot,
                target,
            )
        base, twin = StagingNames(job, "code_chunks"), StagingNames(job, "code_chunks_prefix")
        # Replace only this test's private input with a nonempty arithmetic fixture.
        await client.execute(f"TRUNCATE TABLE loom.{base.snapshot} SYNC")
        await client.execute(
            f"INSERT INTO loom.{base.snapshot} (_loom_rebuild_ordinal, org, namespace, "
            "embedding_model, path, chunk_id, content, embedding, embedding_dim) "
            "VALUES (1, 'test', 'prefix', 'old', 'a.py', {id:UUID}, 'source', [3,4,0], 3)",
            {"id": str(uuid4())},
        )
        await client.execute(
            f"INSERT INTO loom.{base.target} SELECT * REPLACE ('new-model' AS embedding_model) "
            f"FROM loom.{base.snapshot}"
        )
        # Other manifest entries are metadata placeholders, never accessed by this helper.
        manifest = RebuildManifest(
            job,
            "a" * 64,
            tuple(
                plans[table]
                if table in plans
                else RebuildTablePlan(
                    table, uuid4(), "MergeTree", ("value",), "b" * 64, uuid4(), uuid4()
                )
                for table in REBUILD_TABLES
            ),
            prefix_dimensions=(1, 2),
        )
        for _ in range(2):
            checkpoint = await rebuild_snapshot_page(
                twin,
                schema,
                plans["code_chunks_prefix"].snapshot_uuid,
                plans["code_chunks_prefix"].target_uuid,
                "a" * 64,
                SimpleNamespace(model_name="new-model", dim=3),
                after=0,
                manifest=manifest,
                ch=client,
            )
            assert checkpoint.done and checkpoint.rows_written == 2 and checkpoint.cursor == 0
        actual = await client.query_all(
            f"SELECT embedding_model, embedding_dim, embedding, path FROM loom.{twin.target} FINAL "
            "ORDER BY embedding_dim"
        )
        assert actual[0] == {
            "embedding_model": "prefix:1:new-model",
            "embedding_dim": 1,
            "embedding": [1.0],
            "path": "a.py",
        }
        assert actual[1]["embedding"] == pytest.approx([0.6, 0.8], abs=0.005)
        assert actual[1]["embedding_model"] == "prefix:2:new-model"
        await client.execute(
            f"INSERT INTO loom.{twin.target} SELECT * REPLACE ('corrupt.py' AS path) "
            f"FROM loom.{twin.target} FINAL"
        )
        with pytest.raises(ValueError, match="projection differs"):
            await verify_staged_prefixes(
                manifest, model="new-model", dimensions=3, prefix_dimensions=(1, 2), ch=client
            )
        assert (
            await derive_staged_prefixes(
                replace(manifest, prefix_dimensions=()),
                model="new-model",
                dimensions=3,
                prefix_dimensions=(),
                ch=client,
            )
            == 0
        )
    finally:
        try:
            for table, identity in reversed(owned.items()):
                current = await client.query_one(
                    "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                    {"name": table},
                )
                if current and UUID(str(current["uuid"])) == identity:
                    await client.execute(f"DROP TABLE loom.{table} SYNC")
        finally:
            await client.disconnect()
