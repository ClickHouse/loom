"""Cache invalidation is destructive only to its exact private rebuild target."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.store import provider_cache as cache
from server.insights.store.provider_staging import SourceSchema, StagingNames, completion_marker
from server.insights.store.provider_target import target_marker


@pytest.fixture
def plan(monkeypatch):
    names = StagingNames(uuid4(), "embedding_cache")
    schema = SourceSchema(uuid4(), "ReplacingMergeTree", ("model", "embedding"), "a" * 64)
    snapshot, target = uuid4(), uuid4()
    fingerprint = "b" * 64
    source = AsyncMock(return_value={"uuid": snapshot, "comment": completion_marker(names, schema)})
    staged = AsyncMock(
        return_value={
            "uuid": target,
            "comment": target_marker(names, snapshot, fingerprint),
            "engine": "ReplacingMergeTree",
        }
    )
    monkeypatch.setattr(cache, "_owned_snapshot", source)
    monkeypatch.setattr(cache, "_target", staged)
    client = AsyncMock()
    client.query_one.return_value = {"rows": 0}
    return (names, schema, snapshot, target, fingerprint), client, source, staged


@pytest.mark.asyncio
async def test_invalidation_never_truncates_live_cache(plan):
    args, client, *_ = plan
    await cache.invalidate_staged_cache(*args, ch=client)
    client.execute.assert_awaited_once_with(f"TRUNCATE TABLE loom.{args[0].target} SYNC")
    assert await cache.verify_staged_cache(*args, ch=client) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["table", "snapshot", "receipt", "target", "owner"])
async def test_bad_ownership_never_truncates(plan, damage):
    args, client, source, staged = plan
    if damage == "table":
        args = (StagingNames(args[0].job_id, "memory_embeddings"), *args[1:])
    elif damage == "snapshot":
        source.return_value["uuid"] = uuid4()
    elif damage == "receipt":
        source.return_value["comment"] = "incomplete"
    elif damage == "target":
        staged.return_value["uuid"] = uuid4()
    else:
        staged.return_value["comment"] = "another job"
    with pytest.raises(ValueError):
        await cache.invalidate_staged_cache(*args, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [None, {}, {"rows": True}, {"rows": 1}, {"rows": -1}])
async def test_verification_requires_a_confirmed_empty_target(plan, count):
    args, client, *_ = plan
    client.query_one.return_value = count
    with pytest.raises(ValueError, match="incomplete or unknown"):
        await cache.verify_staged_cache(*args, ch=client)
    client.execute.assert_not_awaited()
