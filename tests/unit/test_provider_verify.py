"""Publication requires a complete and internally consistent verification result."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.store.provider_staging import SourceSchema, StagingNames, completion_marker
from server.insights.store.provider_target import target_marker
from server.insights.store.provider_verify import (
    verify_live_key_uniqueness,
    verify_reembedded_target,
)


@pytest.fixture
def verification():
    names = StagingNames(uuid4(), "memory_embeddings")
    schema = SourceSchema(
        uuid4(),
        "ReplacingMergeTree",
        ("embedding_model", "embedding", "memory_key_embedding", "verified", "tombstoned_at"),
        "a" * 64,
    )
    snapshot, target = uuid4(), uuid4()
    result = {
        "source_rows": 3,
        "target_rows": 3,
        "missing": 0,
        "metadata_errors": 0,
        "vector_errors": 0,
    }
    client = AsyncMock()
    client.query_one.side_effect = [
        {"uuid": snapshot, "comment": completion_marker(names, schema)},
        {
            "uuid": target,
            "comment": target_marker(names, snapshot, "b" * 64),
            "engine": "ReplacingMergeTree",
        },
        result,
        {"uuid": schema.table_uuid, "engine": schema.engine, "sorting_key": "verified"},
        {"collisions": 0},
    ]
    return (names, schema, snapshot, target, "b" * 64), client, result


@pytest.mark.asyncio
async def test_complete_verified_target_returns_logical_count(verification):
    args, client, _ = verification
    assert await verify_reembedded_target(*args, model="target", dimensions=3, ch=client) == 3
    sql, params = client.query_one.await_args_list[2].args
    assert "FULL OUTER JOIN" in sql and "FINAL" in sql
    assert "isNotDistinctFrom(s.`verified`, t.`verified`)" in sql
    assert "isNotDistinctFrom(s.`tombstoned_at`, t.`tombstoned_at`)" in sql
    assert "memory_key_embedding" in sql and "isFinite" in sql
    assert params == {"model": "target", "dim": 3, "held": []}


@pytest.mark.asyncio
async def test_held_ordinals_reach_the_empty_vector_check(verification):
    args, client, _ = verification
    await verify_reembedded_target(*args, model="target", dimensions=3, held=[4, 9], ch=client)
    sql, params = client.query_one.await_args_list[2].args
    assert params["held"] == [4, 9]
    assert "NOT has({held:Array(UInt64)}, s._loom_rebuild_ordinal)" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["missing", "metadata_errors", "vector_errors"])
async def test_any_error_blocks_publication(verification, field):
    args, client, result = verification
    result[field] = 1
    with pytest.raises(ValueError, match="failed verification"):
        await verify_reembedded_target(*args, model="target", dimensions=3, ch=client)


@pytest.mark.asyncio
async def test_different_row_counts_block_publication(verification):
    args, client, result = verification
    result["target_rows"] = 2
    with pytest.raises(ValueError, match="failed verification"):
        await verify_reembedded_target(*args, model="target", dimensions=3, ch=client)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-1, True, "0", None])
async def test_unknown_or_malformed_counts_are_not_success(verification, bad):
    args, client, result = verification
    result["vector_errors"] = bad
    with pytest.raises(ValueError, match="Cannot establish"):
        await verify_reembedded_target(*args, model="target", dimensions=3, ch=client)


@pytest.mark.asyncio
async def test_different_ordinals_with_same_live_key_block_publication(verification):
    args, client, _ = verification
    names, schema = args[:2]
    client.query_one.side_effect = [
        {"uuid": schema.table_uuid, "engine": schema.engine, "sorting_key": "verified"},
        {"collisions": 1},
    ]
    with pytest.raises(ValueError, match="collide"):
        await verify_live_key_uniqueness(names, schema, client)
    (sql,) = client.query_one.await_args.args
    assert "GROUP BY `verified` HAVING count() > 1" in sql
    assert "_loom_rebuild_ordinal" not in sql
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "lower(verified)", "verified; DROP TABLE loom.memory"])
async def test_unrecognized_sorting_expression_is_not_interpolated(verification, key):
    args, client, _ = verification
    names, schema = args[:2]
    client.query_one.side_effect = [
        {"uuid": schema.table_uuid, "engine": schema.engine, "sorting_key": key},
    ]
    with pytest.raises(ValueError, match="Unsupported"):
        await verify_live_key_uniqueness(names, schema, client)
    client.query_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_replaced_live_table_blocks_key_verification(verification):
    args, client, _ = verification
    names, schema = args[:2]
    client.query_one.side_effect = [
        {"uuid": uuid4(), "engine": schema.engine, "sorting_key": "verified"},
    ]
    with pytest.raises(ValueError, match="identity changed"):
        await verify_live_key_uniqueness(names, schema, client)
    client.query_one.assert_awaited_once()
