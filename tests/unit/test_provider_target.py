"""Staged pages are complete, bounded, source-specific and retry-safe."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.store.provider_staging import SourceSchema, StagingNames, completion_marker
from server.insights.store.provider_target import ensure_target, target_marker, write_target_page


@pytest.fixture
def target():
    names = StagingNames(uuid4(), "memory_embeddings")
    schema = SourceSchema(
        uuid4(), "ReplacingMergeTree", ("content_excerpt", "verified", "embedding_model"), "b" * 64
    )
    snapshot, identity = uuid4(), uuid4()
    row = {
        "content_excerpt": "keep",
        "verified": 1,
        "embedding_model": "target-model",
        "_loom_rebuild_ordinal": 1,
    }
    client = AsyncMock()
    client.query_one.return_value = {
        "uuid": identity,
        "engine": "ReplacingMergeTree",
        "comment": target_marker(names, snapshot, "a" * 64),
    }
    return names, schema, snapshot, identity, row, client


@pytest.mark.asyncio
async def test_write_preserves_all_columns_and_forces_synchronous_insert(target):
    names, schema, snapshot, identity, row, client = target
    client.query_one.side_effect = [
        client.query_one.return_value,
        {"uuid": snapshot, "comment": completion_marker(names, schema)},
    ]
    await write_target_page(names, schema, snapshot, identity, "a" * 64, [row], after=0, ch=client)
    (sql,) = client.execute.await_args.args
    assert "source.`content_excerpt` AS `content_excerpt`" in sql
    assert "source.`verified` AS `verified`" in sql
    assert "updates.`embedding_model` AS `embedding_model`" in sql
    assert "SETTINGS async_insert = 0" in sql
    external = client.execute.await_args.kwargs["external_data"]
    assert json.loads(external.files[0].data) == {
        "_loom_rebuild_ordinal": 1,
        "embedding_model": "target-model",
    }
    assert "content_excerpt" not in external.files[0].structure
    assert "provider_updates AS updates" in sql
    client.insert.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"value": "missing metadata"}, {"extra": 1}])
async def test_write_rejects_incomplete_or_extra_columns(target, bad):
    names, schema, snapshot, identity, _, client = target
    with pytest.raises(ValueError, match="every source column"):
        await write_target_page(
            names, schema, snapshot, identity, "a" * 64, [bad], after=0, ch=client
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinal", [0, 2, True, "1"])
async def test_write_rejects_non_contiguous_or_invalid_ordinals(target, ordinal):
    names, schema, snapshot, identity, row, client = target
    row["_loom_rebuild_ordinal"] = ordinal
    with pytest.raises(ValueError, match="ordinals"):
        await write_target_page(
            names, schema, snapshot, identity, "a" * 64, [row], after=0, ch=client
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_replaced_target_is_never_written(target):
    names, schema, snapshot, identity, row, client = target
    client.query_one.return_value["uuid"] = uuid4()
    with pytest.raises(ValueError, match="identity changed"):
        await write_target_page(
            names, schema, snapshot, identity, "a" * 64, [row], after=0, ch=client
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_for_different_embedding_space_is_rejected(target):
    names, schema, snapshot, identity, row, client = target
    with pytest.raises(ValueError, match="not owned"):
        await write_target_page(
            names, schema, snapshot, identity, "c" * 64, [row], after=0, ch=client
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_target_copies_schema_without_copying_old_vectors(target):
    names, schema, snapshot, identity, _, client = target
    existing = client.query_one.return_value
    client.query_one.side_effect = [
        {"uuid": snapshot, "comment": completion_marker(names, schema)},
        None,
        existing,
    ]
    assert await ensure_target(names, schema, snapshot, "a" * 64, ch=client) == identity
    (sql,) = client.execute.await_args.args
    assert "AS SELECT" not in sql
    assert "ENGINE = ReplacingMergeTree ORDER BY _loom_rebuild_ordinal" in sql


@pytest.mark.asyncio
async def test_unconfirmed_snapshot_cannot_seed_a_target(target):
    names, schema, snapshot, _, _, client = target
    client.query_one.return_value = {"uuid": snapshot, "comment": names.marker(schema.table_uuid)}
    with pytest.raises(ValueError, match="completed source"):
        await ensure_target(names, schema, snapshot, "a" * 64, ch=client)
    client.execute.assert_not_awaited()
