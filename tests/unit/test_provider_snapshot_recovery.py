"""Only durable completion receipts make source copies reusable after a crash."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.store import provider_staging as staging


@pytest.fixture
def recovery(monkeypatch):
    names = staging.StagingNames(uuid4(), "memory_embeddings")
    schema = staging.SourceSchema(uuid4(), "ReplacingMergeTree", ("value",), "a" * 64)
    old, new = uuid4(), uuid4()
    calls = {
        "source_schema": AsyncMock(return_value=schema),
        "snapshot_query_running": AsyncMock(return_value=False),
        "snapshot_identity": AsyncMock(return_value=old),
        "_owned_snapshot": AsyncMock(
            return_value={"uuid": old, "comment": names.marker(schema.table_uuid)}
        ),
        "discard_snapshot": AsyncMock(return_value=True),
        "create_snapshot": AsyncMock(return_value=new),
        "complete_snapshot": AsyncMock(),
    }
    for name, mock in calls.items():
        monkeypatch.setattr(staging, name, mock)
    return names, schema, old, new, calls


@pytest.mark.asyncio
async def test_unreceipted_copy_is_rebuilt_not_adopted(recovery):
    names, schema, old, new, calls = recovery
    client = AsyncMock()
    assert await staging.ensure_snapshot(names, schema, ch=client) == new
    calls["discard_snapshot"].assert_awaited_once_with(names, schema.table_uuid, old, ch=client)
    calls["complete_snapshot"].assert_awaited_once_with(names, schema, new, ch=client)


@pytest.mark.asyncio
async def test_completed_copy_survives_lost_application_checkpoint(recovery):
    names, schema, old, _, calls = recovery
    calls["_owned_snapshot"].return_value["comment"] = staging.completion_marker(names, schema)
    assert await staging.ensure_snapshot(names, schema, ch=AsyncMock()) == old
    for name in ("discard_snapshot", "create_snapshot", "complete_snapshot"):
        calls[name].assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_for_different_schema_is_not_reused_or_deleted(recovery):
    names, schema, _, _, calls = recovery
    calls["_owned_snapshot"].return_value["comment"] = (
        names.marker(schema.table_uuid) + ":complete:" + "b" * 64
    )
    with pytest.raises(ValueError, match="different source schema"):
        await staging.ensure_snapshot(names, schema, ch=AsyncMock())
    calls["discard_snapshot"].assert_not_awaited()
    calls["create_snapshot"].assert_not_awaited()


@pytest.mark.asyncio
async def test_create_failure_never_writes_completion_receipt(recovery):
    names, schema, _, _, calls = recovery
    calls["snapshot_identity"].return_value = None
    calls["create_snapshot"].side_effect = RuntimeError("interrupted copy")
    with pytest.raises(RuntimeError, match="interrupted"):
        await staging.ensure_snapshot(names, schema, ch=AsyncMock())
    calls["complete_snapshot"].assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_completion_write_is_not_reported_as_success(recovery):
    names, schema, _, _, calls = recovery
    calls["snapshot_identity"].return_value = None
    calls["complete_snapshot"].side_effect = RuntimeError("receipt response lost")
    with pytest.raises(RuntimeError, match="response lost"):
        await staging.ensure_snapshot(names, schema, ch=AsyncMock())


@pytest.mark.asyncio
async def test_live_copy_is_not_adopted_or_removed(recovery):
    names, schema, _, _, calls = recovery
    calls["snapshot_query_running"].return_value = True
    with pytest.raises(ValueError, match="still running"):
        await staging.ensure_snapshot(names, schema, ch=AsyncMock())
    calls["snapshot_identity"].assert_not_awaited()
    calls["discard_snapshot"].assert_not_awaited()


@pytest.mark.parametrize("fingerprint", ["", "x" * 64, "a'", "a" * 63])
def test_completion_marker_only_accepts_sha256_fingerprint(fingerprint):
    names = staging.StagingNames(uuid4(), "memory_embeddings")
    schema = staging.SourceSchema(uuid4(), "MergeTree", ("value",), fingerprint)
    with pytest.raises(ValueError, match="fingerprint"):
        staging.completion_marker(names, schema)
