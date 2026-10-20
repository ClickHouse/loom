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
        "emptied": 0,
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
    assert params == {"model": "target", "dim": 3}


_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def _emptied_rows(texts: list[str]) -> list[list[dict]]:
    """The two reads of one page: the target's empty ordinals, then their snapshot text.
    The fixture's table has two vector columns; the first is the one left empty."""
    ordinals = [4 + i for i in range(len(texts))]
    return [
        [{"ordinal": o, "empty": [True, False]} for o in ordinals],
        [
            {"ordinal": o, "text": t, "real": [True, True], "live": True}
            for o, t in zip(ordinals, texts, strict=True)
        ],
    ]


@pytest.mark.asyncio
async def test_an_emptied_passage_that_reads_as_a_credential_verifies(verification):
    args, client, result = verification
    result["emptied"] = 2
    client.query_all.side_effect = _emptied_rows([f"key {_KEY}", f"also {_KEY}"])
    count = await verify_reembedded_target(
        *args, model="target", dimensions=3, passage_text="content", ch=client
    )
    assert count == 3
    target_sql, source_sql = (c.args[0] for c in client.query_all.await_args_list)
    # A page of the target's empty ordinals, then the snapshot text of those only.
    assert "_loom_rebuild_ordinal > {after:UInt64}" in target_sql and "LIMIT" in target_sql
    assert "IN {ordinals:Array(UInt64)}" in source_sql and "`content` AS text" in source_sql
    assert "JOIN" not in target_sql + source_sql
    assert client.query_all.await_args_list[1].args[1] == {"ordinals": [4, 5]}


@pytest.mark.asyncio
async def test_an_emptied_plain_passage_fails_verification(verification):
    args, client, result = verification
    result["emptied"] = 2
    client.query_all.side_effect = _emptied_rows([f"key {_KEY}", "a plain turn"])
    with pytest.raises(ValueError, match="failed verification"):
        await verify_reembedded_target(
            *args, model="target", dimensions=3, passage_text="content", ch=client
        )


@pytest.mark.asyncio
async def test_the_emptied_ordinals_are_read_a_page_at_a_time(verification, monkeypatch):
    from server.insights.store import provider_verify

    monkeypatch.setattr(provider_verify, "_EMPTIED_PAGE", 2)
    args, client, result = verification
    result["emptied"] = 3
    first, first_text = _emptied_rows([f"key {_KEY}", f"key {_KEY}"])
    client.query_all.side_effect = [
        first,
        first_text,
        [{"ordinal": 12, "empty": [True, False]}],
        [{"ordinal": 12, "text": f"key {_KEY}", "real": [True, True], "live": True}],
    ]
    count = await verify_reembedded_target(
        *args, model="target", dimensions=3, passage_text="content", ch=client
    )
    assert count == 3
    afters = [c.args[1].get("after") for c in client.query_all.await_args_list]
    assert afters == [0, None, 5, None]


@pytest.mark.asyncio
async def test_an_emptied_row_fails_a_table_that_holds_no_passages(verification):
    args, client, result = verification
    result["emptied"] = 1
    with pytest.raises(ValueError, match="failed verification"):
        await verify_reembedded_target(*args, model="target", dimensions=3, ch=client)
    client.query_all.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["missing", "metadata_errors", "vector_errors", "emptied"])
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
