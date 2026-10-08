"""Snapshots are explicit full-row copies with stable cursors and owned identities."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.store import provider_staging as staging


@pytest.fixture
def source():
    names = staging.StagingNames(uuid4(), "memory_embeddings")
    table_uuid, snapshot_uuid = uuid4(), uuid4()
    client = AsyncMock()
    client.query_one.return_value = {
        "uuid": table_uuid,
        "engine": "ReplacingMergeTree",
        "create_table_query": "source ddl",
    }
    client.query_all.return_value = [
        {"name": "content_excerpt", "type": "String", "default_kind": "", "default_expression": ""},
        {"name": "verified", "type": "UInt8", "default_kind": "DEFAULT", "default_expression": "0"},
        {
            "name": "content_excerpt_lc",
            "type": "String",
            "default_kind": "MATERIALIZED",
            "default_expression": "lower(content_excerpt)",
        },
    ]
    for name in (
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "scope",
        "memory_key",
        "version",
        "embedding_model",
        "tombstoned_at",
    ):
        client.query_all.return_value.append(
            {"name": name, "type": "String", "default_kind": "", "default_expression": ""}
        )
    return names, table_uuid, snapshot_uuid, client


@pytest.mark.asyncio
async def test_schema_carries_all_writable_columns_but_not_materialized_columns(source):
    names, table_uuid, _, client = source
    schema = await staging.source_schema(names, ch=client)
    assert schema.table_uuid == table_uuid
    assert schema.columns[:2] == ("content_excerpt", "verified")
    assert "content_excerpt_lc" not in schema.columns
    assert schema.final == " FINAL"
    assert len(schema.fingerprint) == 64


@pytest.mark.asyncio
async def test_snapshot_creation_never_silently_accepts_an_existing_partial_copy(source):
    names, source_uuid, snapshot_uuid, client = source
    schema = await staging.source_schema(names, ch=client)
    original = client.query_one.return_value
    client.query_one.side_effect = [
        original,
        {"uuid": snapshot_uuid, "comment": names.marker(source_uuid)},
    ]
    assert await staging.create_snapshot(names, schema, ch=client) == snapshot_uuid
    (sql,) = client.execute.await_args.args
    assert "IF NOT EXISTS" not in sql
    assert "row_number() OVER ()" in sql
    assert "`content_excerpt`, `verified`" in sql
    assert "FROM loom.memory_embeddings FINAL ORDER BY `version` DESC" in sql
    assert "isNull(`tombstoned_at`) ASC, `embedding_model` ASC LIMIT 1 BY" in sql
    assert f"COMMENT '{names.marker(source_uuid)}'" in sql
    assert "{marker:String}" not in sql


@pytest.mark.asyncio
async def test_schema_change_prevents_creating_a_mismatched_copy(source):
    names, _, _, client = source
    schema = await staging.source_schema(names, ch=client)
    client.query_one.return_value["create_table_query"] = "changed source ddl"
    with pytest.raises(ValueError, match="schema changed"):
        await staging.create_snapshot(names, schema, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_uses_journaled_identity_and_bounded_ordinal_not_offset(source):
    names, source_uuid, snapshot_uuid, client = source
    schema = await staging.source_schema(names, ch=client)
    client.query_one.return_value = {"uuid": snapshot_uuid, "comment": names.marker(source_uuid)}
    client.query_all.return_value = [
        {"_loom_rebuild_ordinal": 5, "content_excerpt": "test", "verified": 1}
    ]
    result = await staging.snapshot_page(names, schema, snapshot_uuid, after=4, limit=10, ch=client)
    assert result[0]["_loom_rebuild_ordinal"] == 5
    sql, params = client.query_all.await_args.args
    assert "OFFSET" not in sql
    assert "LIMIT {limit:UInt32}" in sql
    assert params == {"after": 4, "limit": 10}


@pytest.mark.asyncio
async def test_replaced_snapshot_cannot_be_read_as_a_valid_resume_target(source):
    names, source_uuid, snapshot_uuid, client = source
    schema = await staging.source_schema(names, ch=client)
    client.query_one.return_value = {"uuid": uuid4(), "comment": names.marker(source_uuid)}
    client.query_all.reset_mock()
    with pytest.raises(ValueError, match="identity changed"):
        await staging.snapshot_page(names, schema, snapshot_uuid, after=0, ch=client)
    client.query_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_unowned_same_name_table_cannot_be_read(source):
    names, _, snapshot_uuid, client = source
    schema = await staging.source_schema(names, ch=client)
    client.query_one.return_value = {"uuid": snapshot_uuid, "comment": "not this job"}
    with pytest.raises(ValueError, match="not owned"):
        await staging.snapshot_page(names, schema, snapshot_uuid, after=0, ch=client)


@pytest.mark.parametrize("table", ["memory", "events", "memory_embeddings; DROP DATABASE loom", ""])
def test_staging_names_cannot_address_non_rebuild_tables(table):
    with pytest.raises(ValueError):
        staging.StagingNames(uuid4(), table)


@pytest.mark.asyncio
async def test_discard_removes_only_owned_expected_copy(source):
    names, source_uuid, snapshot_uuid, client = source
    client.query_one.side_effect = [
        {"running": 0},
        {"uuid": snapshot_uuid, "comment": names.marker(source_uuid)},
    ]
    assert await staging.discard_snapshot(names, source_uuid, snapshot_uuid, ch=client)
    client.execute.assert_awaited_once_with(f"DROP TABLE loom.{names.snapshot} SYNC")


@pytest.mark.asyncio
async def test_discard_missing_copy_is_idempotent(source):
    names, source_uuid, snapshot_uuid, client = source
    client.query_one.side_effect = [{"running": 0}, None]
    assert not await staging.discard_snapshot(names, source_uuid, snapshot_uuid, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_discard_rejects_replaced_copy(source):
    names, source_uuid, snapshot_uuid, client = source
    client.query_one.side_effect = [
        {"running": 0},
        {"uuid": uuid4(), "comment": names.marker(source_uuid)},
    ]
    with pytest.raises(ValueError, match="identity changed"):
        await staging.discard_snapshot(names, source_uuid, snapshot_uuid, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_discard_rejects_copy_from_another_source(source):
    names, source_uuid, snapshot_uuid, client = source
    client.query_one.side_effect = [
        {"running": 0},
        {"uuid": snapshot_uuid, "comment": names.marker(uuid4())},
    ]
    with pytest.raises(ValueError, match="not owned"):
        await staging.discard_snapshot(names, source_uuid, snapshot_uuid, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_inspection_does_not_adopt_or_modify_an_unjournaled_copy(source):
    names, source_uuid, snapshot_uuid, client = source
    client.query_one.return_value = {"uuid": snapshot_uuid, "comment": names.marker(source_uuid)}
    assert await staging.snapshot_identity(names, source_uuid, ch=client) == snapshot_uuid
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, {}, {"running": -1}, {"running": True}])
async def test_unknown_query_status_never_allows_discard(source, status):
    names, source_uuid, snapshot_uuid, client = source
    client.query_one.return_value = status
    with pytest.raises(ValueError, match="Cannot establish"):
        await staging.discard_snapshot(names, source_uuid, snapshot_uuid, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_create_blocks_discard_even_before_table_appears(source):
    names, source_uuid, snapshot_uuid, client = source
    client.query_one.return_value = {"running": 1}
    with pytest.raises(ValueError, match="still running"):
        await staging.discard_snapshot(names, source_uuid, snapshot_uuid, ch=client)
    client.query_one.assert_awaited_once()
    client.execute.assert_not_awaited()
