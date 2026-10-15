"""Rebuild every live vector axis while keeping source and row identity intact."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.insights.app.provider_rebuild_batch import (
    SPECIAL_TEXT_TABLES,
    TEXT_COLUMNS,
    RebuildBatchError,
    rebuild_batch,
)
from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES


@pytest.fixture
def target():
    async def embed(texts):
        return [[0.6, 0.8] for _ in texts]

    return SimpleNamespace(
        model_name="target-model", dim=2, embed_many=AsyncMock(side_effect=embed)
    )


def row_for(table):
    row = {
        "org": "private-org",
        "namespace": "private-ns",
        "embedding_model": "old-model",
        "embedding_dim": 3,
        "version": 123,
        "tombstoned_at": None,
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "verified": 1,
        "custom_future_metadata": {"preserve": ["this"]},
    }
    for surface in EMBEDDING_SURFACES:
        if surface.table == table:
            row[surface.column] = [1.0, 0.0, 0.0]
    if table in TEXT_COLUMNS:
        row[TEXT_COLUMNS[table]] = "source text"
    if table == "memory_embeddings":
        row.update(memory_key="user_dog_name", memory_key_embedding_dim=3)
    if table == "code_chunks":
        row.update(context_text="contextual source", content="raw source")
    if table == "categories":
        row.update(name="Category", description="Description")
    return row


def test_text_recipes_cover_every_reembed_surface():
    tables = {surface.table for surface in EMBEDDING_SURFACES if surface.action == "reembed"}
    assert set(TEXT_COLUMNS) | SPECIAL_TEXT_TABLES == tables


@pytest.mark.asyncio
@pytest.mark.parametrize("table", sorted(set(TEXT_COLUMNS) | SPECIAL_TEXT_TABLES))
async def test_all_tables_preserve_full_rows_without_mutating_source(table, target):
    row = row_for(table)
    before = deepcopy(row)
    result = await rebuild_batch(table, [row], target, source_texts={0: "tracker source"})
    assert row == before
    assert len(result) == 1
    assert result[0]["embedding_model"] == "target-model"
    assert result[0]["embedding_dim"] == 2
    changed = {"embedding_model", "embedding_dim", "memory_key_embedding_dim"} | {
        surface.column for surface in EMBEDDING_SURFACES if surface.table == table
    }
    assert set(result[0]) == set(row)
    assert {k: v for k, v in result[0].items() if k not in changed} == {
        k: v for k, v in before.items() if k not in changed
    }


@pytest.mark.asyncio
async def test_memory_key_uses_the_same_text_recipe_as_normal_writes(target):
    result = await rebuild_batch("memory_embeddings", [row_for("memory_embeddings")], target)
    target.embed_many.assert_awaited_once_with(["source text", "user dog name"])
    assert result[0]["memory_key_embedding"] == [0.6, 0.8]
    assert result[0]["memory_key_embedding_dim"] == 2


@pytest.mark.asyncio
async def test_code_uses_context_and_falls_back_to_content(target):
    contextual = row_for("code_chunks")
    raw = {**contextual, "context_text": ""}
    await rebuild_batch("code_chunks", [contextual, raw], target)
    target.embed_many.assert_awaited_once_with(["contextual source", "raw source"])


@pytest.mark.asyncio
async def test_tombstones_never_send_deleted_content_to_provider(target):
    row = row_for("memory_embeddings")
    row["tombstoned_at"] = datetime(2025, 2, 1, tzinfo=UTC)
    result = (await rebuild_batch("memory_embeddings", [row], target))[0]
    target.embed_many.assert_not_awaited()
    assert result["tombstoned_at"] == row["tombstoned_at"]
    assert result["embedding"] == result["memory_key_embedding"] == []
    assert result["embedding_dim"] == result["memory_key_embedding_dim"] == 0


@pytest.mark.asyncio
async def test_tracker_source_must_be_hydrated_before_any_billable_request(target):
    with pytest.raises(RebuildBatchError, match="source_text_missing"):
        await rebuild_batch("qa_item_embeddings", [row_for("qa_item_embeddings")], target)
    target.embed_many.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vectors,code",
    [
        ([], "embedding_count_mismatch"),
        ([[1.0]], "embedding_dimension_mismatch"),
        ([[float("nan"), 1.0]], "embedding_vector_invalid"),
        ([[float("inf"), 1.0]], "embedding_vector_invalid"),
        ([[0.0, 0.0]], "embedding_vector_invalid"),
        ([[True, False]], "embedding_vector_invalid"),
        ([[1e39, 1.0]], "embedding_vector_invalid"),
        ([["private text", 1.0]], "embedding_vector_invalid"),
        ([None], "embedding_vector_invalid"),
        (None, "embedding_vector_invalid"),
    ],
)
async def test_invalid_provider_output_cannot_be_published(target, vectors, code):
    target.embed_many.side_effect = None
    target.embed_many.return_value = vectors
    row = row_for("chunks")
    before = deepcopy(row)
    with pytest.raises(RebuildBatchError, match=code):
        await rebuild_batch("chunks", [row], target)
    assert row == before


@pytest.mark.asyncio
async def test_provider_failure_is_safe_and_does_not_modify_source(target):
    target.embed_many.side_effect = RuntimeError("provider body containing a secret")
    row = row_for("chunks")
    before = deepcopy(row)
    with pytest.raises(RebuildBatchError) as error:
        await rebuild_batch("chunks", [row], target)
    assert str(error.value) == "embedding_provider_failed"
    assert row == before


@pytest.mark.asyncio
async def test_provider_requests_are_bounded(target):
    rows = [row_for("chunks") for _ in range(130)]
    result = await rebuild_batch("chunks", rows, target)
    assert len(result) == 130
    assert [len(call.args[0]) for call in target.embed_many.await_args_list] == [64, 64, 2]


@pytest.mark.asyncio
async def test_late_batch_failure_cannot_publish_earlier_vectors(target):
    rows = [row_for("chunks") for _ in range(65)]
    before = deepcopy(rows)
    target.embed_many.side_effect = [[[0.6, 0.8]] * 64, RuntimeError("upstream error")]
    with pytest.raises(RebuildBatchError, match="embedding_provider_failed"):
        await rebuild_batch("chunks", rows, target)
    assert rows == before


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_as_provider_failure(target):
    target.embed_many.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await rebuild_batch("chunks", [row_for("chunks")], target)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [[], [0.0, 0.0, 0.0]], ids=["empty", "placeholder"])
async def test_empty_unindexed_text_stays_unindexed_in_target_space(target, stored):
    row = {**row_for("chunks"), "content": "", "embedding": stored, "embedding_dim": 0}
    result = (await rebuild_batch("chunks", [row], target))[0]
    assert result["embedding_model"] == "target-model"
    assert result["embedding"] == []
    assert result["embedding_dim"] == 0
    target.embed_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_structural_summary_card_without_text_rebuilds_as_a_placeholder(target):
    """A file the summary worker gave no card keeps the graph pass's row: no summary,
    and the zero vector the insert path pads a coded column with."""
    from server.code.store.index import build_row
    from server.core.vectors.placeholders import pad_rows

    row = build_row("code_summaries", org="o", namespace="n", embedding_model="old-model")
    row.update(scope_path="tests/__init__.py", pagerank=0.1)
    assert pad_rows("code_summaries", [row]) == [(0, "embedding")]
    assert row["embedding"] and not any(row["embedding"])
    result = (await rebuild_batch("code_summaries", [row], target))[0]
    assert result["embedding_model"] == "target-model"
    assert (result["embedding"], result["embedding_dim"], result["pagerank"]) == ([], 0, 0.1)
    target.embed_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_source_text_cannot_silently_discard_an_existing_vector(target):
    row = {**row_for("chunks"), "content": ""}
    with pytest.raises(RebuildBatchError, match="source_text_missing"):
        await rebuild_batch("chunks", [row], target)
    target.embed_many.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["events", "embedding_cache", "code_chunks_prefix", "unknown"])
async def test_non_reembed_actions_cannot_accidentally_send_data(target, table):
    with pytest.raises(RebuildBatchError, match="unsupported_rebuild_table"):
        await rebuild_batch(table, [], target)
    target.embed_many.assert_not_awaited()
