"""Canonical rebuild inputs choose current records across old model labels."""

import os
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from server.core.repo_base import _default_client
from server.core.vectors.placeholders import placeholder
from server.insights.store.provider_staging import StagingNames, _source_selection, source_schema
from server.settings import settings


@pytest.mark.asyncio
async def test_latest_full_row_and_equal_revision_deletion_win_across_models():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    client = _default_client()
    await client.connect()
    names = StagingNames(uuid4(), "memory_embeddings")
    table = names.snapshot
    identity = None
    try:
        schema = await source_schema(names, ch=client)
        await client.execute(f"CREATE TABLE loom.{table} AS loom.memory_embeddings")
        created = await client.query_one(
            "SELECT uuid FROM system.tables WHERE database='loom' AND name={table:String}",
            {"table": table},
        )
        identity = UUID(str(created["uuid"]))
        deleted = datetime(2026, 10, 5)
        rows = [
            ("updated", "old text", "old-a", 10, None, 0),
            ("updated", "current text", "old-b", 20, None, 1),
            ("deleted", "old text", "old-a", 20, None, 0),
            ("deleted", "", "old-b", 20, deleted, 0),
            ("restored", "", "old-a", 20, deleted, 0),
            ("restored", "explicit restore", "old-b", 30, None, 1),
        ]
        vector = placeholder(settings.embedding_dim)  # a coded column takes this width only
        await client.insert(
            f"loom.{table}",
            [
                {
                    "org": "test",
                    "namespace": "ns",
                    "agent": "agent",
                    "user_id": "-",
                    "session_id": "-",
                    "scope": "private",
                    "memory_type": "semantic",
                    "memory_key": key,
                    "content_excerpt": text,
                    "embedding_model": model,
                    "version": version,
                    "tombstoned_at": tombstoned,
                    "verified": verified,
                    "embedding": vector,
                    "memory_key_embedding": vector,
                }
                for key, text, model, version, tombstoned, verified in rows
            ],
        )
        selection = _source_selection(names, schema).replace(
            "FROM loom.memory_embeddings FINAL", f"FROM loom.{table} FINAL"
        )
        rows = await client.query_all(f"SELECT * FROM ({selection})")
        actual = {row["memory_key"]: row for row in rows}
        assert len(rows) == len(actual) == 3
        assert actual["updated"]["content_excerpt"] == "current text"
        assert actual["updated"]["verified"] == 1
        assert actual["deleted"]["tombstoned_at"] is not None
        assert actual["deleted"]["content_excerpt"] == ""
        assert actual["restored"]["tombstoned_at"] is None
        assert actual["restored"]["content_excerpt"] == "explicit restore"
    finally:
        try:
            if identity is not None:
                current = await client.query_one(
                    "SELECT uuid FROM system.tables WHERE database='loom' AND name={table:String}",
                    {"table": table},
                )
                if current and UUID(str(current["uuid"])) == identity:
                    await client.execute(f"DROP TABLE loom.{table} SYNC")
        finally:
            await client.disconnect()
