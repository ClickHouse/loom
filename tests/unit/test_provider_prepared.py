"""Replacement candidates retain live DDL and never adopt incomplete copies."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.domain.provider_prepared_ddl import PreparedDDL
from server.insights.store import provider_prepared as prepared
from server.insights.store.provider_staging import SourceSchema, StagingNames, completion_marker
from server.insights.store.provider_target import target_marker


@pytest.fixture
def plan(monkeypatch):
    names = StagingNames(uuid4(), "memory_embeddings")
    schema = SourceSchema(uuid4(), "ReplacingMergeTree", ("value", "verified"), "a" * 64)
    snapshot, target, candidate = uuid4(), uuid4(), uuid4()
    fingerprint = "b" * 64
    marker = f"{completion_marker(names, schema)}:publish:{target}:{fingerprint}"
    client = AsyncMock()
    client.query_one.return_value = {
        "create_table_query": "CREATE TABLE loom.memory_embeddings "
        "(`value` String, `verified` UInt8) ENGINE = ReplacingMergeTree ORDER BY value"
    }
    monkeypatch.setattr(prepared, "source_schema", AsyncMock(return_value=schema))
    monkeypatch.setattr(
        prepared,
        "_target",
        AsyncMock(
            return_value={
                "uuid": target,
                "engine": "ReplacingMergeTree",
                "comment": target_marker(names, snapshot, fingerprint),
            }
        ),
    )
    monkeypatch.setattr(prepared, "_assert_idle", AsyncMock())
    monkeypatch.setattr(prepared, "_count", AsyncMock(return_value=3))
    monkeypatch.setattr(prepared, "_verify_copy", AsyncMock())
    observed = AsyncMock(side_effect=[None, {"uuid": candidate, "comment": marker}])
    monkeypatch.setattr(prepared, "_prepared", observed)
    return (names, schema, snapshot, target, fingerprint), candidate, marker, client, observed


@pytest.mark.asyncio
async def test_clone_preserves_live_schema_and_omits_staging_ordinal(plan):
    args, candidate, marker, client, _ = plan
    assert (
        await prepared.prepare_live_table(*args, expected_rows=3, dimensions=8, ch=client)
        == candidate
    )
    statements = [call.args[0] for call in client.execute.await_args_list]
    assert "ENGINE = ReplacingMergeTree ORDER BY value" in statements[0]
    assert "(`value`, `verified`)" in statements[1]
    assert "_loom_rebuild_ordinal" not in statements[1]
    assert "FINAL SETTINGS async_insert = 0" in statements[1]
    assert f"{marker}:rows:3" in statements[2]


@pytest.mark.asyncio
async def test_confirmed_candidate_is_reused_without_writes(plan):
    args, candidate, marker, client, observed = plan
    observed.side_effect = [{"uuid": candidate, "comment": marker + ":rows:3"}]
    assert (
        await prepared.prepare_live_table(*args, expected_rows=3, dimensions=8, ch=client)
        == candidate
    )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_candidate_is_discarded_before_recopy(plan):
    args, candidate, marker, client, observed = plan
    observed.side_effect = [
        {"uuid": uuid4(), "comment": marker},
        {"uuid": candidate, "comment": marker},
    ]
    await prepared.prepare_live_table(*args, expected_rows=3, dimensions=8, ch=client)
    assert client.execute.await_args_list[0].args[0] == f"DROP TABLE loom.{args[0].prepared} SYNC"


@pytest.mark.asyncio
async def test_unowned_candidate_is_never_removed(plan):
    args, _, _, client, observed = plan
    observed.side_effect = [{"uuid": uuid4(), "comment": "another plan"}]
    with pytest.raises(ValueError, match="not owned"):
        await prepared.prepare_live_table(*args, expected_rows=3, dimensions=8, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_row_loss_never_gets_a_completion_receipt(plan, monkeypatch):
    args, _, _, client, _ = plan
    monkeypatch.setattr(prepared, "_count", AsyncMock(return_value=2))
    with pytest.raises(ValueError, match="row count"):
        await prepared.prepare_live_table(*args, expected_rows=3, dimensions=8, ch=client)
    assert not any("MODIFY COMMENT" in call.args[0] for call in client.execute.await_args_list)


@pytest.mark.asyncio
async def test_replication_paths_are_never_blindly_cloned(plan):
    args, _, _, client, _ = plan
    names, schema, *rest = args
    with pytest.raises(ValueError, match="replication plan"):
        await prepared.prepare_live_table(
            names,
            replace(schema, engine="ReplicatedReplacingMergeTree"),
            *rest,
            expected_rows=3,
            dimensions=8,
            ch=client,
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_same_count_content_change_is_rejected(plan, monkeypatch, existing):
    args, candidate, marker, client, observed = plan
    if existing:
        observed.side_effect = [{"uuid": candidate, "comment": marker + ":rows:3"}]
    monkeypatch.setattr(
        prepared, "_verify_copy", AsyncMock(side_effect=ValueError("content differs"))
    )
    with pytest.raises(ValueError, match="content differs"):
        await prepared.prepare_live_table(*args, expected_rows=3, dimensions=8, ch=client)
    assert not any("MODIFY COMMENT" in call.args[0] for call in client.execute.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"mismatches": True}, {"mismatches": -1}])
async def test_content_comparison_fails_closed_when_unknown(result):
    client = AsyncMock()
    client.query_one.return_value = result
    names = StagingNames(uuid4(), "memory_embeddings")
    schema = SourceSchema(uuid4(), "ReplacingMergeTree", ("value", "verified"), "a" * 64)
    with pytest.raises(ValueError, match="Cannot establish"):
        await prepared._verify_copy(names, schema, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("available", ["enable_quantized_codec", "allow_experimental_codecs"])
async def test_coded_create_uses_the_available_setting_and_preserves_chained_codec(available):
    client = AsyncMock()
    client.query_all.return_value = [{"name": available}]
    plan = PreparedDDL(
        "CREATE TABLE candidate (embedding Array(BFloat16) CODEC(Quantized('rabitq', 8, 0), LZ4))",
        ("embedding",),
    )
    await prepared._create_candidate(plan, client)
    client.execute.assert_awaited_once_with(
        plan.sql, settings={available: 1, "allow_suspicious_codecs": 1}
    )


@pytest.mark.asyncio
async def test_unknown_codec_setting_never_executes_ddl():
    client = AsyncMock()
    client.query_all.return_value = []
    with pytest.raises(ValueError, match="setting is unavailable"):
        await prepared._create_candidate(PreparedDDL("coded", ("embedding",)), client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_equality_checks_the_physical_placeholder_projection():
    client = AsyncMock()
    client.query_one.return_value = {"mismatches": 0}
    names = StagingNames(uuid4(), "memory_embeddings")
    schema = SourceSchema(uuid4(), "MergeTree", ("embedding", "embedding_dim"), "a" * 64)
    projection = PreparedDDL("coded", ("embedding",)).projection(schema.columns, 8)
    await prepared._verify_copy(names, schema, client, projection=projection)
    sql = client.query_one.await_args.args[0]
    assert f"SELECT tuple({projection}) AS row, toInt64(-1)" in sql
    assert "SELECT tuple(`embedding`, `embedding_dim`) AS row, toInt64(1)" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatches", [0, 1])
async def test_content_comparison_uses_full_row_multisets(mismatches):
    client = AsyncMock()
    client.query_one.return_value = {"mismatches": mismatches}
    names = StagingNames(uuid4(), "memory_embeddings")
    schema = SourceSchema(uuid4(), "ReplacingMergeTree", ("value", "verified"), "a" * 64)
    if mismatches:
        with pytest.raises(ValueError, match="content differs"):
            await prepared._verify_copy(names, schema, client)
    else:
        await prepared._verify_copy(names, schema, client)
    sql = client.query_one.await_args.args[0]
    assert "tuple(`value`, `verified`)" in sql
    assert "UNION ALL" in sql
    assert "HAVING sum(delta) != 0" in sql
    assert "_loom_rebuild_ordinal" not in sql
