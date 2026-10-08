"""Recovery inventory uses physical UUIDs, not staging-name prefixes."""

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES, REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.store.provider_rebuild import rebuild_inventory
from server.insights.store.provider_staging import (
    StagingNames,
    discard_snapshot,
    ensure_snapshot,
    source_schema,
)


@pytest.mark.asyncio
async def test_inventory_checks_staging_uuid_on_live_clickhouse():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    client = _default_client()
    await client.connect()
    names = StagingNames(uuid4(), "memory_embeddings")
    snapshot = None
    try:
        schema = await source_schema(names, ch=client)
        assert await rebuild_inventory(ch=client) == EMBEDDING_SURFACES
        snapshot = await ensure_snapshot(names, schema, ch=client)
        # Only this snapshot exists. Other journal identities are placeholders:
        # this test proves exclusion, not manifest-wide table existence.
        tables = tuple(
            RebuildTablePlan(
                table,
                schema.table_uuid if table == names.table else uuid4(),
                schema.engine,
                schema.columns,
                schema.fingerprint,
                snapshot if table == names.table else uuid4(),
                uuid4(),
            )
            for table in REBUILD_TABLES
        )
        manifest = RebuildManifest(names.job_id, "a" * 64, tables)
        with pytest.raises(ValueError, match="unknown="):
            await rebuild_inventory(ch=client)
        assert await rebuild_inventory(manifest=manifest, ch=client) == EMBEDDING_SURFACES
        wrong = replace(
            manifest,
            tables=tuple(
                replace(table, snapshot_uuid=uuid4()) if table.table == names.table else table
                for table in manifest.tables
            ),
        )
        with pytest.raises(ValueError, match="replaced staging"):
            await rebuild_inventory(manifest=wrong, ch=client)
    finally:
        try:
            if snapshot is not None:
                await discard_snapshot(names, schema.table_uuid, snapshot, ch=client)
        finally:
            await client.disconnect()
