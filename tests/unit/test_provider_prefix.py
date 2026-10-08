"""Prefix derivation is a projection of verified staging, never a provider call."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.store import provider_prefix as prefix
from server.insights.store.provider_staging import StagingNames


@pytest.fixture
def plan(monkeypatch):
    manifest = RebuildManifest(
        uuid4(),
        "a" * 64,
        tuple(
            RebuildTablePlan(
                table,
                uuid4(),
                "ReplacingMergeTree",
                ("path", "embedding_model", "embedding", "embedding_dim"),
                "b" * 64,
                uuid4(),
                uuid4(),
            )
            for table in REBUILD_TABLES
        ),
        prefix_dimensions=(1, 2),
    )
    client = AsyncMock()
    client.query_one.side_effect = [{"rows": 6}, {"mismatches": 0}, {"mismatches": 0}]
    monkeypatch.setattr(prefix, "verify_reembedded_target", AsyncMock(return_value=3))
    monkeypatch.setattr(
        prefix,
        "_owned_prefix",
        AsyncMock(return_value=StagingNames(manifest.job_id, "code_chunks_prefix")),
    )
    return manifest, client


@pytest.mark.asyncio
async def test_derivation_replaces_only_private_target_and_verifies_every_projection(plan):
    manifest, client = plan
    assert (
        await prefix.derive_staged_prefixes(
            manifest, model="new-model", dimensions=3, prefix_dimensions=(1, 2), ch=client
        )
        == 6
    )
    sql = [call.args[0] for call in client.execute.await_args_list]
    assert (
        sql[0]
        == f"TRUNCATE TABLE loom.{StagingNames(manifest.job_id, 'code_chunks_prefix').target} SYNC"
    )
    assert all("async_insert = 0" in query for query in sql[1:])
    assert [call.args[1]["label"] for call in client.execute.await_args_list[1:]] == [
        "prefix:1:new-model",
        "prefix:2:new-model",
    ]
    assert "GROUP BY row HAVING sum(delta) != 0" in client.query_one.await_args.args[0]


@pytest.mark.asyncio
async def test_empty_configuration_clears_old_twins(plan):
    manifest, client = plan
    manifest = replace(manifest, prefix_dimensions=())
    client.query_one.side_effect = [{"rows": 0}]
    assert (
        await prefix.derive_staged_prefixes(
            manifest, model="new-model", dimensions=3, prefix_dimensions=(), ch=client
        )
        == 0
    )
    client.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("dimensions", [(0,), (4,), (True,), (1, 1), [1]])
async def test_invalid_projection_never_truncates(plan, dimensions):
    manifest, client = plan
    with pytest.raises(ValueError, match="projection plan"):
        await prefix.derive_staged_prefixes(
            manifest, model="new-model", dimensions=3, prefix_dimensions=dimensions, ch=client
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepared_targets_cannot_be_rebuilt(plan):
    manifest, client = plan
    manifest = replace(
        manifest,
        tables=tuple(
            replace(table, prepared_uuid=uuid4(), verified_rows=3)
            if table.table == "code_chunks"
            else table
            for table in manifest.tables
        ),
    )
    with pytest.raises(ValueError, match="cannot be regenerated"):
        await prefix.derive_staged_prefixes(
            manifest, model="new-model", dimensions=3, prefix_dimensions=(1, 2), ch=client
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_base_never_truncates(plan):
    manifest, client = plan
    prefix.verify_reembedded_target.side_effect = ValueError("bad base")
    with pytest.raises(ValueError, match="bad base"):
        await prefix.derive_staged_prefixes(
            manifest, model="new-model", dimensions=3, prefix_dimensions=(1, 2), ch=client
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_count_corruption_fails_verification(plan):
    manifest, client = plan
    manifest = replace(manifest, prefix_dimensions=(1,))
    client.query_one.side_effect = [{"rows": 3}, {"mismatches": 1}]
    with pytest.raises(ValueError, match="projection differs"):
        await prefix.verify_staged_prefixes(
            manifest, model="new-model", dimensions=3, prefix_dimensions=(1,), ch=client
        )
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_runtime_policy_cannot_override_durable_plan(plan):
    manifest, client = plan
    with pytest.raises(ValueError, match="durable policy"):
        await prefix.derive_staged_prefixes(
            manifest, model="new-model", dimensions=3, prefix_dimensions=(1,), ch=client
        )
    client.execute.assert_not_awaited()
