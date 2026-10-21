"""Opt-in snapshot SQL checks against a disposable, migrated ClickHouse database."""

import asyncio
import os
from uuid import uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.app.provider_rebuild_batch import TEXT_COLUMNS
from server.insights.app.provider_rebuild_page import PAGE_SIZE, rebuild_snapshot_page
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.store.provider_prepared import prepare_live_table
from server.insights.store.provider_staging import (
    StagingNames,
    create_snapshot,
    discard_snapshot,
    ensure_snapshot,
    session_chunks,
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

# More than two pages, so a rebuild that stops after one is caught.
_SEEDED = 2 * PAGE_SIZE + 5
# Columns a re-embed never changes: the rebuilt row must carry them as snapshotted.
_PRESERVED = ("org", "namespace", "memory_key", "content_excerpt", "content_sha256", "created_at")


def _width():
    """The install's vector width: coded tables (26.8+) hold no other."""
    from server.settings import settings

    return int(settings.embedding_dim)


async def _seed(client, namespace):
    from server.memory.store.rows import build_embedding_row
    from server.settings import settings

    rows = [
        build_embedding_row(
            org="staging-test",
            namespace=namespace,
            agent="a",
            memory_key=f"k{i:03d}",
            content_sha256=f"{i:064x}",
            content_excerpt=f"staged row {i}",
            embedding=[1.0] + [0.0] * (settings.embedding_dim - 1),
            memory_key_embedding=[1.0] + [0.0] * (settings.embedding_dim - 1),
            embedding_model="seed-model",
        )
        for i in range(_SEEDED)
    ]
    await client.insert("loom.memory_embeddings", rows, settings={"async_insert": 0})


async def _rebuild_all(names, schema, identity, target_uuid, client):
    """Every page of the snapshot, as the coordinator steps through it."""
    cursor = written = 0
    while True:
        checkpoint = await rebuild_snapshot_page(
            names, schema, identity, target_uuid, "a" * 64, _TestEmbedder(), after=cursor, ch=client
        )
        written += checkpoint.rows_written
        cursor = checkpoint.cursor
        if checkpoint.done:
            return written


_OPT_IN = "Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable, freshly migrated database"


async def _require_empty(client, table: str) -> None:
    """A rebuild stages its whole table, so rows other tests left would fail it;
    the opt-in promises a fresh database, so say so rather than skip."""
    row = await client.query_one(f"SELECT count() AS n FROM loom.{table}")
    if row is not None and int(row["n"]):
        pytest.fail(f"loom.{table} holds rows; this test needs a freshly migrated database")


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
        pytest.skip(_OPT_IN)
    client = _default_client()
    await client.connect()
    await _require_empty(client, table)
    names = StagingNames(uuid4(), table)
    identity = None
    target_uuid = None
    prepared_uuid = None
    seeded_ns = f"staging-{names.job_id.hex[:12]}"
    try:
        if table == "memory_embeddings":
            await _seed(client, seeded_ns)
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
            dimensions=_width(),
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
                dimensions=_width(),
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
        if table == "memory_embeddings":
            snapshotted = (
                await client.query_one(f"SELECT count() AS n FROM loom.{names.snapshot}")
            )["n"]
            assert snapshotted >= _SEEDED
            written = await _rebuild_all(names, schema, identity, target_uuid, client)
            assert written == snapshotted
            regenerated = await client.query_all(
                f"SELECT embedding_model, length(embedding) AS dim FROM loom.{names.target} FINAL"
            )
            assert len(regenerated) == written
            cols = ", ".join(_PRESERVED)
            ordered = "WHERE namespace = {ns:String} ORDER BY _loom_rebuild_ordinal"
            rebuilt = await client.query_all(
                f"SELECT {cols} FROM loom.{names.target} FINAL {ordered}", {"ns": seeded_ns}
            )
            source = await client.query_all(
                f"SELECT {cols} FROM loom.{names.snapshot} {ordered}", {"ns": seeded_ns}
            )
            assert len(rebuilt) == _SEEDED and rebuilt == source
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
                == written
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
            assert await _rebuild_all(names, schema, identity, target_uuid, client) == written
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
        if table == "memory_embeddings":
            await client.execute(
                "ALTER TABLE loom.memory_embeddings DELETE WHERE namespace = {ns:String} "
                "SETTINGS mutations_sync = 2",
                {"ns": seeded_ns},
            )
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
        pytest.skip(_OPT_IN)
    from server.settings import settings

    client = _default_client()
    await client.connect()
    await _require_empty(client, "memory_embeddings")
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
        pytest.skip(_OPT_IN)
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


async def _drop_owned(client, name, uuid):
    current = await client.query_one(
        "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
        {"name": name},
    )
    if current is not None and str(current["uuid"]) == str(uuid):
        await client.execute(f"DROP TABLE loom.{name} SYNC")


@pytest.mark.asyncio
async def test_a_text_less_summary_card_rebuilds_verifies_and_publishes_as_a_placeholder():
    """Right after a backfill a file's summary card is the graph pass's structural row:
    no summary, and the zero vector the insert path pads the vector column with."""
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip(_OPT_IN)
    from server.code.store.index import CodeRepo, build_row

    client = _default_client()
    await client.connect()
    await _require_empty(client, "code_summaries")
    org, namespace = "rebuild-placeholder", f"code-{uuid4().hex[:8]}"
    names = StagingNames(uuid4(), "code_summaries")
    embedder = _EightDimEmbedder()
    identity = target_uuid = prepared_uuid = None
    try:
        card = build_row(
            "code_summaries",
            org=org,
            namespace=namespace,
            embedding_model="old-model",
            scope_path="tests/__init__.py",
            pagerank=0.5,
        )
        await CodeRepo(client).insert_rows("code_summaries", [card])
        schema = await source_schema(names, ch=client)
        identity = await ensure_snapshot(names, schema, ch=client)
        target_uuid = await ensure_target(names, schema, identity, "a" * 64, ch=client)
        checkpoint = await rebuild_snapshot_page(
            names, schema, identity, target_uuid, "a" * 64, embedder, after=0, ch=client
        )
        assert checkpoint.done and checkpoint.rows_written == 1
        rows = await verify_reembedded_target(
            names, schema, identity, target_uuid, "a" * 64, model=embedder.model_name,
            dimensions=8, ch=client,
        )  # fmt: skip
        prepared_uuid = await prepare_live_table(
            names, schema, identity, target_uuid, "a" * 64,
            expected_rows=rows, dimensions=8, ch=client,
        )  # fmt: skip
        codec = await client.query_one(
            "SELECT compression_codec FROM system.columns WHERE database='loom' "
            "AND table={t:String} AND name='embedding'",
            {"t": names.prepared},
        )
        published = await client.query_one(
            f"SELECT embedding_model, embedding, embedding_dim, pagerank FROM loom.{names.prepared} "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            {"org": org, "ns": namespace},
        )
        assert published["embedding_model"] == embedder.model_name
        assert (published["embedding_dim"], published["pagerank"]) == (0, 0.5)
        # A coded column takes its placeholder at the new width; a plain one stays empty.
        coded = codec is not None and "Quantized" in str(codec["compression_codec"])
        assert published["embedding"] == ([0.0] * 8 if coded else [])
    finally:
        for name, uuid in (
            (names.prepared, prepared_uuid),
            (names.target, target_uuid),
            (names.snapshot, identity),
        ):
            if uuid is not None:
                await _drop_owned(client, name, uuid)
        await client.execute(
            "DELETE FROM loom.code_summaries WHERE org = {org:String} AND namespace = {ns:String}",
            {"org": org, "ns": namespace},
        )
        await client.disconnect()


def _passage_rows(table: str, org: str, namespace: str, texts: list[str]) -> list[dict]:
    """Rows an earlier build embedded, one per text, under the old model."""
    vector = [1.0] + [0.0] * (_width() - 1)
    if table == "memory_chunks":
        return [
            {
                "org": org,
                "namespace": namespace,
                "agent": "a",
                "session_id": "s",
                "chunk_index": i,
                "content_sha256": f"{i:064x}",
                "content": text,
                "embedding": vector,
                "embedding_model": "old-model",
                "embedding_dim": _width(),
            }
            for i, text in enumerate(texts)
        ]
    return [
        {
            "org": org,
            "namespace": namespace,
            "agent": "a",
            "thread_id": "t",
            "msg_id": f"m{i}",
            "role": "user",
            "content_excerpt": text,
            "embedding": vector,
            "embedding_model": "old-model",
            "embedding_dim": _width(),
        }
        for i, text in enumerate(texts)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["memory_chunks", "conversation_embeddings"])
async def test_a_credential_passage_is_staged_empty_and_verifies(table):
    """A passage an earlier build embedded whose text reads as a credential is rebuilt
    without a vector, as its write path leaves it; verification accepts that row and no
    other empty one."""
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip(_OPT_IN)
    client = _default_client()
    await client.connect()
    await _require_empty(client, table)
    org, namespace = "rebuild-held", f"held-{uuid4().hex[:8]}"
    names = StagingNames(uuid4(), table)
    embedder = _EightDimEmbedder()
    identity = target_uuid = None
    try:
        key = "sk-" + "proj-" + uuid4().hex + uuid4().hex[:8]
        await client.insert(
            f"loom.{table}",
            _passage_rows(table, org, namespace, ["a plain turn", f"the key is {key}"]),
            settings={"async_insert": 0},
        )
        schema = await source_schema(names, ch=client)
        identity = await ensure_snapshot(names, schema, ch=client)
        target_uuid = await ensure_target(names, schema, identity, "a" * 64, ch=client)
        checkpoint = await rebuild_snapshot_page(
            names, schema, identity, target_uuid, "a" * 64, embedder, after=0, ch=client
        )
        assert checkpoint.done and checkpoint.rows_written == 2
        args = (names, schema, identity, target_uuid, "a" * 64)
        # Read as a table that holds no passages, the empty row is an error.
        with pytest.raises(ValueError, match="failed verification"):
            await verify_reembedded_target(
                *args, model=embedder.model_name, dimensions=8, ch=client
            )
        rows = await verify_reembedded_target(
            *args,
            model=embedder.model_name,
            dimensions=8,
            text_column=TEXT_COLUMNS[table],
            ch=client,
        )
        assert rows == 2
    finally:
        for name, uuid in ((names.target, target_uuid), (names.snapshot, identity)):
            if uuid is not None:
                await _drop_owned(client, name, uuid)
        await client.execute(
            f"DELETE FROM loom.{table} WHERE org = {{org:String}} AND namespace = {{ns:String}}",
            {"org": org, "ns": namespace},
        )
        await client.disconnect()


@pytest.mark.asyncio
async def test_a_plain_chunk_stored_without_a_vector_reads_its_session_and_is_embedded():
    """A plain memory chunk stored without a vector between two embedded ones makes the
    rebuild read its session's chunks (`session_chunks`) on a migrated ClickHouse; they
    touch no key, so it is embedded."""
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip(_OPT_IN)
    client = _default_client()
    await client.connect()
    await _require_empty(client, "memory_chunks")
    org, namespace = "rebuild-held", f"held-{uuid4().hex[:8]}"
    names = StagingNames(uuid4(), "memory_chunks")
    embedder = _EightDimEmbedder()
    identity = target_uuid = None
    try:
        rows = _passage_rows(
            "memory_chunks", org, namespace, ["plain words before", "plain middle", "after"]
        )
        rows[1].update(embedding=[0.0] * _width(), embedding_dim=0)
        await client.insert("loom.memory_chunks", rows, settings={"async_insert": 0})
        stored = await session_chunks(org, namespace, "s", 0, 2, ch=client)
        assert sorted((r["chunk_index"], r["embedding_dim"]) for r in stored) == [
            (0, _width()),
            (1, 0),
            (2, _width()),
        ]
        schema = await source_schema(names, ch=client)
        identity = await ensure_snapshot(names, schema, ch=client)
        target_uuid = await ensure_target(names, schema, identity, "a" * 64, ch=client)
        checkpoint = await rebuild_snapshot_page(
            names, schema, identity, target_uuid, "a" * 64, embedder, after=0, ch=client
        )
        assert checkpoint.done and checkpoint.rows_written == 3
        staged = await client.query_all(
            f"SELECT chunk_index, embedding_dim FROM loom.{names.target} FINAL ORDER BY chunk_index"
        )
        assert [(r["chunk_index"], r["embedding_dim"]) for r in staged] == [
            (0, 8),
            (1, 8),
            (2, 8),
        ]
    finally:
        for name, uuid in ((names.target, target_uuid), (names.snapshot, identity)):
            if uuid is not None:
                await _drop_owned(client, name, uuid)
        await client.execute(
            "DELETE FROM loom.memory_chunks WHERE org = {org:String} AND namespace = {ns:String}",
            {"org": org, "ns": namespace},
        )
        await client.disconnect()
