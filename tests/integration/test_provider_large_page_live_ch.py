"""Production-size vector pages use multipart files, not limited form fields."""

import math
import os
from uuid import UUID, uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.store.provider_staging import SourceSchema, StagingNames, completion_marker
from server.insights.store.provider_target import ensure_target, write_target_page


@pytest.mark.asyncio
async def test_dense_3072_dimension_page_preserves_server_metadata_and_retry_identity():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    client = _default_client()
    await client.connect()
    names = StagingNames(uuid4(), "code_chunks")
    columns = ("org", "namespace", "content", "at", "embedding_model", "embedding_dim", "embedding")
    schema = SourceSchema(uuid4(), "MergeTree", columns, "a" * 64)
    owned = {}
    try:
        await client.execute(
            f"CREATE TABLE loom.{names.snapshot} (_loom_rebuild_ordinal UInt64, "
            "org String, namespace String, content String, at DateTime64(9, 'UTC'), "
            "embedding_model String, embedding_dim UInt32, embedding Array(BFloat16)) "
            "ENGINE=MergeTree ORDER BY _loom_rebuild_ordinal "
            f"COMMENT '{completion_marker(names, schema)}'"
        )
        row = await client.query_one(
            "SELECT uuid FROM system.tables WHERE database='loom' AND name={table:String}",
            {"table": names.snapshot},
        )
        snapshot = UUID(str(row["uuid"]))
        owned[names.snapshot] = snapshot
        await client.execute(
            f"INSERT INTO loom.{names.snapshot} SELECT number+1, 'fixture', 'large-page', "
            "'preserve source text', toDateTime64('2026-10-05 12:34:56.123456789',9,'UTC'), "
            "'old-model', 0, [] FROM numbers(64) SETTINGS async_insert=0"
        )
        target = await ensure_target(names, schema, snapshot, "b" * 64, ch=client)
        owned[names.target] = target
        vector = [1 / math.sqrt(3072)] * 3072
        rows = [
            {
                "_loom_rebuild_ordinal": index,
                "org": "fixture",
                "namespace": "large-page",
                "content": "do not roundtrip metadata",
                "at": None,
                "embedding_model": "new-model",
                "embedding_dim": 3072,
                "embedding": vector,
            }
            for index in range(1, 65)
        ]
        for _ in range(2):
            await write_target_page(
                names, schema, snapshot, target, "b" * 64, rows, after=0, ch=client
            )
        result = await client.query_one(
            "SELECT count() AS rows, min(embedding_dim) AS dimension, min(length(embedding)) AS width, "
            "uniqExact(toString(at)) AS stamps, any(toString(at)) AS stamp, "
            "uniqExact(content) AS texts, any(content) AS text FROM "
            f"loom.{names.target} FINAL"
        )
        assert result == {
            "rows": 64,
            "dimension": 3072,
            "width": 3072,
            "stamps": 1,
            "stamp": "2026-10-05 12:34:56.123456789",
            "texts": 1,
            "text": "preserve source text",
        }
    finally:
        try:
            for table, identity in owned.items():
                row = await client.query_one(
                    "SELECT uuid FROM system.tables WHERE database='loom' AND name={table:String}",
                    {"table": table},
                )
                if row and UUID(str(row["uuid"])) == identity:
                    await client.execute(f"DROP TABLE loom.{table} SYNC")
        finally:
            await client.disconnect()
