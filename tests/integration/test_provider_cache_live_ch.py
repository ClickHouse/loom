"""Cache rebuild clears staging without touching currently served cache rows."""

import os
from uuid import UUID, uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.app.provider_rebuild_page import rebuild_snapshot_page
from server.insights.store.provider_cache import verify_staged_cache
from server.insights.store.provider_staging import (
    StagingNames,
    discard_snapshot,
    ensure_snapshot,
    source_schema,
)
from server.insights.store.provider_target import ensure_target


@pytest.mark.asyncio
async def test_cache_rebuild_is_empty_idempotent_and_keeps_live_rows():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    client = _default_client()
    await client.connect()
    names = StagingNames(uuid4(), "embedding_cache")
    snapshot = target = None
    try:
        schema = await source_schema(names, ch=client)
        before = await client.query_all(
            "SELECT * FROM loom.embedding_cache FINAL ORDER BY model, key"
        )
        snapshot = await ensure_snapshot(names, schema, ch=client)
        target = await ensure_target(names, schema, snapshot, "a" * 64, ch=client)
        await client.execute(
            f"INSERT INTO loom.{names.target} (_loom_rebuild_ordinal, model, key, dim, embedding) "
            "VALUES (1, 'old-model', 'old-endpoint-cache-entry', 3, [1, 0, 0])"
        )
        with pytest.raises(ValueError, match="incomplete"):
            await verify_staged_cache(names, schema, snapshot, target, "a" * 64, ch=client)
        for _ in range(2):
            # A deliberately unusable embedder proves this path needs no provider.
            checkpoint = await rebuild_snapshot_page(
                names, schema, snapshot, target, "a" * 64, object(), after=0, ch=client
            )
            assert checkpoint.done and checkpoint.rows_written == checkpoint.cursor == 0
            assert (
                await verify_staged_cache(names, schema, snapshot, target, "a" * 64, ch=client) == 0
            )
        assert (
            await client.query_all("SELECT * FROM loom.embedding_cache FINAL ORDER BY model, key")
            == before
        )
    finally:
        try:
            if target is not None:
                current = await client.query_one(
                    "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                    {"name": names.target},
                )
                if current and UUID(str(current["uuid"])) == target:
                    await client.execute(f"DROP TABLE loom.{names.target} SYNC")
            if snapshot is not None:
                await discard_snapshot(names, schema.table_uuid, snapshot, ch=client)
        finally:
            await client.disconnect()
