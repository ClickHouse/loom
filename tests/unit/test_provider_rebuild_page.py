"""Provider or staging errors never produce an advanced rebuild checkpoint."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.app import provider_rebuild_page as page
from server.insights.store.provider_staging import SourceSchema, StagingNames


@pytest.fixture
def plan(monkeypatch):
    args = (
        StagingNames(uuid4(), "memory_embeddings"),
        SourceSchema(uuid4(), "ReplacingMergeTree", ("value",), "a" * 64),
        uuid4(),
        uuid4(),
        "b" * 64,
        object(),
    )
    read = AsyncMock(return_value=[{"value": "source", "_loom_rebuild_ordinal": 5}])
    embed = AsyncMock(return_value=[{"value": "rebuilt", "_loom_rebuild_ordinal": 5}])
    write = AsyncMock()
    monkeypatch.setattr(page, "snapshot_page", read)
    monkeypatch.setattr(page, "rebuild_batch", embed)
    monkeypatch.setattr(page, "write_target_page", write)
    return args, read, embed, write


@pytest.mark.asyncio
async def test_checkpoint_follows_successful_staged_write(plan):
    args, read, embed, write = plan
    result = await page.rebuild_snapshot_page(*args, after=4)
    assert (result.cursor, result.rows_written, result.done) == (5, 1, True)
    read.assert_awaited_once_with(args[0], args[1], args[2], after=4, limit=64, ch=None)
    embed.assert_awaited_once_with(
        "memory_embeddings", read.return_value, args[5], source_texts=None, waiting=[], ch=None
    )
    write.assert_awaited_once_with(*args[:5], embed.return_value, after=4, ch=None)


@pytest.mark.asyncio
async def test_provider_failure_never_writes_a_partial_page(plan):
    args, _, embed, write = plan
    embed.side_effect = ValueError("provider failed")
    with pytest.raises(ValueError, match="provider failed"):
        await page.rebuild_snapshot_page(*args, after=4)
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_staging_response_retries_the_same_cursor(plan):
    args, read, _, write = plan
    write.side_effect = [RuntimeError("response lost"), None]
    with pytest.raises(RuntimeError, match="response lost"):
        await page.rebuild_snapshot_page(*args, after=4)
    result = await page.rebuild_snapshot_page(*args, after=4)
    assert result.cursor == 5
    assert [call.kwargs["after"] for call in read.await_args_list] == [4, 4]
    assert [call.kwargs["after"] for call in write.await_args_list] == [4, 4]


@pytest.mark.asyncio
async def test_empty_final_page_needs_no_provider_call_or_insert(plan):
    args, read, embed, write = plan
    read.return_value = []
    result = await page.rebuild_snapshot_page(*args, after=64)
    assert (result.cursor, result.rows_written, result.done) == (64, 0, True)
    embed.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_page_does_not_prematurely_finish_table(plan):
    args, read, _, _ = plan
    read.return_value *= 64
    result = await page.rebuild_snapshot_page(*args, after=4)
    assert (result.cursor, result.rows_written, result.done) == (68, 64, False)


@pytest.mark.asyncio
async def test_cache_uses_invalidation_without_embedding_or_row_copy(plan, monkeypatch):
    args, read, embed, write = plan
    args = (StagingNames(args[0].job_id, "embedding_cache"), *args[1:])
    invalidate = AsyncMock()
    monkeypatch.setattr(page, "invalidate_staged_cache", invalidate)
    result = await page.rebuild_snapshot_page(*args, after=0)
    assert result == page.BatchCheckpoint(0, 0, True)
    invalidate.assert_awaited_once_with(*args[:5], ch=None)
    read.assert_not_awaited()
    embed.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [True, -1, 1])
async def test_cache_rejects_a_row_cursor(plan, monkeypatch, cursor):
    args, *_ = plan
    args = (StagingNames(args[0].job_id, "embedding_cache"), *args[1:])
    invalidate = AsyncMock()
    monkeypatch.setattr(page, "invalidate_staged_cache", invalidate)
    with pytest.raises(ValueError, match="no row cursor"):
        await page.rebuild_snapshot_page(*args, after=cursor)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracker_page_hydrates_before_embedding(plan, monkeypatch):
    args, read, embed, _ = plan
    args = (StagingNames(args[0].job_id, "qa_item_embeddings"), *args[1:])
    hydrate = AsyncMock(return_value=(read.return_value, {0: "Title\nBody"}))
    monkeypatch.setattr(page, "reconcile_tracker_page", hydrate)
    await page.rebuild_snapshot_page(*args, after=4)
    hydrate.assert_awaited_once_with(
        read.return_value, body_chars=page.settings.tracker_embed_body_chars
    )
    embed.assert_awaited_once_with(
        "qa_item_embeddings",
        read.return_value,
        args[5],
        source_texts={0: "Title\nBody"},
        waiting=[],
        ch=None,
    )


@pytest.mark.asyncio
async def test_tracker_hydration_failure_never_calls_provider_or_writes(plan, monkeypatch):
    args, _, embed, write = plan
    args = (StagingNames(args[0].job_id, "qa_item_embeddings"), *args[1:])
    monkeypatch.setattr(
        page, "reconcile_tracker_page", AsyncMock(side_effect=ValueError("source changed"))
    )
    with pytest.raises(ValueError, match="source changed"):
        await page.rebuild_snapshot_page(*args, after=4)
    embed.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefix_requires_durable_projection_plan_before_writes(plan, monkeypatch):
    args, read, embed, write = plan
    args = (StagingNames(args[0].job_id, "code_chunks_prefix"), *args[1:])
    derive = AsyncMock()
    monkeypatch.setattr(page, "derive_staged_prefixes", derive)
    with pytest.raises(ValueError, match="requires a manifest"):
        await page.rebuild_snapshot_page(*args, after=0)
    derive.assert_not_awaited()
    read.assert_not_awaited()
    embed.assert_not_awaited()
    write.assert_not_awaited()
