"""Opt-in snapshot SQL checks against a disposable, migrated ClickHouse database."""

import asyncio
import os
from uuid import uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.app.provider_rebuild_page import rebuild_snapshot_page
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.store.provider_prepared import prepare_live_table
from server.insights.store.provider_staging import (
    StagingNames,
    create_snapshot,
    discard_snapshot,
    ensure_snapshot,
    snapshot_identity,
    snapshot_page,
    snapshot_query_running,
    source_schema,
)
from server.insights.store.provider_target import ensure_target, write_target_page
from server.insights.store.provider_verify import (
    verify_live_key_uniqueness,
    verify_reembedded_target,
)


class _TestEmbedder:
    """Deterministic local test double, not a real-provider quality test."""

    model_name = "rebuild-page-test"
    dim = 3

    async def embed_many(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", REBUILD_TABLES)
async def test_snapshot_sql_and_cursor_on_live_clickhouse(table):
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    client = _default_client()
    await client.connect()
    names = StagingNames(uuid4(), table)
    identity = None
    target_uuid = None
    prepared_uuid = None
    try:
        schema = await source_schema(names, ch=client)
        identity = await create_snapshot(names, schema, ch=client)
        incomplete = identity
        identity = await ensure_snapshot(names, schema, ch=client)
        assert identity != incomplete
        assert await ensure_snapshot(names, schema, ch=client) == identity
        target_uuid = await ensure_target(names, schema, identity, "a" * 64, ch=client)
        assert await ensure_target(names, schema, identity, "a" * 64, ch=client) == target_uuid
        first_page = await snapshot_page(names, schema, identity, after=0, limit=2, ch=client)
        updated_page = [
            {
                **row,
                **({"embedding_model": "rebuild-test-model"} if "embedding_model" in row else {}),
            }
            for row in first_page
        ]
        if first_page:
            for _ in range(2):
                await write_target_page(
                    names, schema, identity, target_uuid, "a" * 64, updated_page, after=0, ch=client
                )
        staged = await client.query_all(
            f"SELECT * FROM loom.{names.target} FINAL ORDER BY _loom_rebuild_ordinal"
        )
        assert len(staged) == len(first_page)
        prepared_uuid = await prepare_live_table(
            names,
            schema,
            identity,
            target_uuid,
            "a" * 64,
            expected_rows=len(staged),
            dimensions=8,
            ch=client,
        )
        assert (
            await prepare_live_table(
                names,
                schema,
                identity,
                target_uuid,
                "a" * 64,
                expected_rows=len(staged),
                dimensions=8,
                ch=client,
            )
            == prepared_uuid
        )
        column_query = (
            "SELECT name, type, default_kind, default_expression FROM system.columns "
            "WHERE database='loom' AND table={table:String} ORDER BY position"
        )
        assert await client.query_all(
            column_query, {"table": names.prepared}
        ) == await client.query_all(column_query, {"table": names.table})
        for actual, expected in zip(staged, updated_page, strict=True):
            assert actual.keys() == expected.keys()
            for column in expected:
                assert actual[column] == expected[column], column
        assert (
            await snapshot_page(names, schema, identity, after=0, limit=2, ch=client) == first_page
        )
        if table == "memory_embeddings" and first_page:
            checkpoint = await rebuild_snapshot_page(
                names,
                schema,
                identity,
                target_uuid,
                "a" * 64,
                _TestEmbedder(),
                after=0,
                ch=client,
            )
            assert checkpoint.rows_written >= len(first_page)
            regenerated = await client.query_all(
                f"SELECT embedding_model, length(embedding) AS dim FROM loom.{names.target} FINAL"
            )
            assert len(regenerated) == checkpoint.rows_written
            assert all(row["embedding_model"] == "rebuild-page-test" for row in regenerated)
            assert all(row["dim"] in (0, 3) for row in regenerated)
            assert (
                await verify_reembedded_target(
                    names,
                    schema,
                    identity,
                    target_uuid,
                    "a" * 64,
                    model="rebuild-page-test",
                    dimensions=3,
                    ch=client,
                )
                == checkpoint.rows_written
            )
            with pytest.raises(ValueError, match="failed verification"):
                await verify_reembedded_target(
                    names,
                    schema,
                    identity,
                    target_uuid,
                    "a" * 64,
                    model="wrong-model",
                    dimensions=3,
                    ch=client,
                )
            # Tombstones correctly rebuild to empty vectors at any requested width.
            # Inject a malformed vector in our private target for this negative test.
            await client.execute(
                f"INSERT INTO loom.{names.target} SELECT * REPLACE "
                f"([1.0, 0.0] AS embedding, 2 AS embedding_dim) FROM loom.{names.target} FINAL"
            )
            with pytest.raises(ValueError, match="failed verification"):
                await verify_reembedded_target(
                    names,
                    schema,
                    identity,
                    target_uuid,
                    "a" * 64,
                    model="rebuild-page-test",
                    dimensions=3,
                    ch=client,
                )
            await rebuild_snapshot_page(
                names,
                schema,
                identity,
                target_uuid,
                "a" * 64,
                _TestEmbedder(),
                after=0,
                ch=client,
            )
            # Deliberately corrupt only this test's disposable staging table.
            await client.execute(
                f"INSERT INTO loom.{names.target} SELECT * REPLACE "
                f"(if(verified = 0, 1, 0) AS verified) FROM loom.{names.target} FINAL"
            )
            with pytest.raises(ValueError, match="failed verification"):
                await verify_reembedded_target(
                    names,
                    schema,
                    identity,
                    target_uuid,
                    "a" * 64,
                    model="rebuild-page-test",
                    dimensions=3,
                    ch=client,
                )
            await verify_live_key_uniqueness(names, schema, client)
            await client.execute(
                f"INSERT INTO loom.{names.target} SELECT * REPLACE "
                f"(_loom_rebuild_ordinal + 100000 AS _loom_rebuild_ordinal) "
                f"FROM loom.{names.target} FINAL LIMIT 1"
            )
            with pytest.raises(ValueError, match="collide"):
                await verify_live_key_uniqueness(names, schema, client)
        cursor = 0
        while page := await snapshot_page(
            names, schema, identity, after=cursor, limit=2, ch=client
        ):
            for row in page:
                assert set(row) == {*schema.columns, "_loom_rebuild_ordinal"}
                assert row["_loom_rebuild_ordinal"] == cursor + 1
                cursor += 1
        count = await client.query_one(f"SELECT count() AS n FROM loom.{names.snapshot}")
        assert cursor == count["n"]
        assert await source_schema(names, ch=client) == schema
        with pytest.raises(ValueError, match="identity changed"):
            await discard_snapshot(names, schema.table_uuid, uuid4(), ch=client)
        assert await snapshot_identity(names, schema.table_uuid, ch=client) == identity
        assert await discard_snapshot(names, schema.table_uuid, identity, ch=client)
        assert not await discard_snapshot(names, schema.table_uuid, identity, ch=client)
        assert await source_schema(names, ch=client) == schema
    finally:
        if prepared_uuid is not None:
            current = await client.query_one(
                "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                {"name": names.prepared},
            )
            if current is not None and str(current["uuid"]) == str(prepared_uuid):
                await client.execute(f"DROP TABLE loom.{names.prepared} SYNC")
        if target_uuid is not None:
            current = await client.query_one(
                "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                {"name": names.target},
            )
            if current is not None and str(current["uuid"]) == str(target_uuid):
                await client.execute(f"DROP TABLE loom.{names.target} SYNC")
        if identity is not None:
            # Remove only the unique table created by this test, never a replacement.
            current = await client.query_one(
                "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                {"name": names.snapshot},
            )
            if current is not None and str(current["uuid"]) == str(identity):
                await client.execute(f"DROP TABLE loom.{names.snapshot} SYNC")
        await client.disconnect()


class _EightDimEmbedder:
    model_name = "rebuild-eight-dim"
    dim = 8

    async def embed_many(self, texts):
        return [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_a_populated_coded_source_rebuilds_at_another_width():
    """Staging must take the new model's width from a coded source table, whose
    copies ClickHouse would otherwise hold to the source codec's width."""
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    from server.settings import settings

    client = _default_client()
    await client.connect()
    codec = await client.query_one(
        "SELECT compression_codec FROM system.columns WHERE database='loom' "
        "AND table='memory_embeddings' AND name='embedding'"
    )
    if codec is None or "Quantized" not in str(codec["compression_codec"]):
        pytest.skip("memory_embeddings is not coded on this server")
    width = settings.embedding_dim
    vector = [1.0] + [0.0] * (width - 1)
    names = StagingNames(uuid4(), "memory_embeddings")
    owned: dict[str, object] = {}
    try:
        await client.insert(
            "loom.memory_embeddings",
            [
                {
                    "org": "coded-rebuild",
                    "namespace": names.job_id.hex,
                    "agent": "a",
                    "memory_key": "k",
                    "memory_type": "semantic",
                    "content_excerpt": "a fact at the old width",
                    "embedding_model": settings.embedding_model,
                    "embedding_dim": width,
                    "embedding": vector,
                    "memory_key_embedding": vector,
                }
            ],
        )
        schema = await source_schema(names, ch=client)
        owned[names.snapshot] = identity = await ensure_snapshot(names, schema, ch=client)
        owned[names.target] = target = await ensure_target(
            names, schema, identity, "b" * 64, ch=client
        )
        embedder = _EightDimEmbedder()
        checkpoint = await rebuild_snapshot_page(
            names, schema, identity, target, "b" * 64, embedder, after=0, ch=client
        )
        assert checkpoint.rows_written >= 1
        await verify_reembedded_target(
            names, schema, identity, target, "b" * 64, model=embedder.model_name, dimensions=8,
            ch=client,
        )  # fmt: skip
        owned[names.prepared] = await prepare_live_table(
            names, schema, identity, target, "b" * 64,
            expected_rows=checkpoint.rows_written, dimensions=8, ch=client,
        )  # fmt: skip
        prepared_codec = await client.query_one(
            "SELECT compression_codec FROM system.columns WHERE database='loom' "
            "AND table={t:String} AND name='embedding'",
            {"t": names.prepared},
        )
        assert prepared_codec is not None and "8, 0" in str(prepared_codec["compression_codec"])
        row = await client.query_one(
            f"SELECT length(embedding) AS width FROM loom.{names.prepared} FINAL "
            "WHERE org='coded-rebuild' AND namespace={ns:String}",
            {"ns": names.job_id.hex},
        )
        assert row is not None and row["width"] == 8
    finally:
        for table, uuid in owned.items():
            current = await client.query_one(
                "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                {"name": table},
            )
            if current is not None and str(current["uuid"]) == str(uuid):
                await client.execute(f"DROP TABLE loom.{table} SYNC")
        await client.execute(
            "DELETE FROM loom.memory_embeddings WHERE org='coded-rebuild'",
        )
        await client.disconnect()


@pytest.mark.asyncio
async def test_cleanup_refuses_a_live_create_query():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    client = _default_client()
    await client.connect()
    names = StagingNames(uuid4(), "memory_embeddings")
    schema = await source_schema(names, ch=client)
    creation = asyncio.create_task(
        client.execute(
            f"CREATE TABLE loom.{names.snapshot} ENGINE = MergeTree ORDER BY value "
            f"COMMENT '{names.marker(schema.table_uuid)}' AS SELECT sleep(2) AS value"
        )
    )
    try:
        for _ in range(30):
            if await snapshot_query_running(names, ch=client):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("The test did not observe its deliberately slow CREATE")
        with pytest.raises(ValueError, match="still running"):
            await discard_snapshot(names, schema.table_uuid, uuid4(), ch=client)
        await creation
        assert not await snapshot_query_running(names, ch=client)
    finally:
        # Let the server finish; cancellation alone is not a database drain.
        await creation
        identity = await snapshot_identity(names, schema.table_uuid, ch=client)
        if identity is not None:
            await discard_snapshot(names, schema.table_uuid, identity, ch=client)
        await client.disconnect()
