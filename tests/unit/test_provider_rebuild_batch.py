"""Rebuild every live vector axis while keeping source and row identity intact."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.insights.app import provider_rebuild_batch
from server.insights.app.provider_rebuild_batch import (
    PASSAGE_TABLES,
    SPECIAL_TEXT_TABLES,
    TEXT_COLUMNS,
    RebuildBatchError,
    rebuild_batch,
)
from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES
from server.memory.domain.okf_evidence import IMPORT_MAX_ITEMS
from server.settings import settings
from tests.unit._fake_credentials import AWS_KEY_ID, OPENAI_KEY


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
    if table == "chunks":
        row.update(context="Handbook\nRefunds", content="Within 14 days.")
    if table == "categories":
        row.update(name="Category", description="Description")
    if table == "memory_chunks":
        row.update(session_id="s", chunk_index=0)
    return row


def test_text_recipes_cover_every_reembed_surface():
    tables = {surface.table for surface in EMBEDDING_SURFACES if surface.action == "reembed"}
    assert set(TEXT_COLUMNS) | SPECIAL_TEXT_TABLES == tables


@pytest.mark.asyncio
@pytest.mark.parametrize("table", sorted(set(TEXT_COLUMNS) | SPECIAL_TEXT_TABLES))
async def test_all_tables_preserve_full_rows_without_mutating_source(table, target):
    row = row_for(table)
    before = deepcopy(row)
    result = (await rebuild_batch(table, [row], target, source_texts={0: "tracker source"})).rows
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
    result = (await rebuild_batch("memory_embeddings", [row_for("memory_embeddings")], target)).rows
    target.embed_many.assert_awaited_once_with(["source text", "user dog name"])
    assert result[0]["memory_key_embedding"] == [0.6, 0.8]
    assert result[0]["memory_key_embedding_dim"] == 2


@pytest.mark.asyncio
async def test_an_import_key_rebuilds_only_the_key_vector_it_holds(target):
    """An import key has no key axis text. Without a key vector it is staged empty; with one
    a build before the import wrote, the migration rebuilds it from the key, not fails."""
    from server.memory.domain.import_keys import import_key

    key = import_key("Prefers dark roast.")
    without = {**row_for("memory_embeddings"), "memory_key": key, "memory_key_embedding": [0.0] * 3}
    stale = {**row_for("memory_embeddings"), "memory_key": key}
    result = (await rebuild_batch("memory_embeddings", [without, stale], target)).rows
    target.embed_many.assert_awaited_once_with(["source text", "source text", key])
    assert [r["memory_key_embedding_dim"] for r in result] == [0, 2]


@pytest.mark.asyncio
async def test_a_key_with_no_axis_text_and_a_vector_still_fails(target):
    """Only an import key gets its stale key vector rebuilt from the raw key."""
    row = {**row_for("memory_embeddings"), "memory_key": "___"}
    with pytest.raises(RebuildBatchError, match="source_text_missing"):
        await rebuild_batch("memory_embeddings", [row], target)


@pytest.mark.asyncio
async def test_code_uses_context_and_falls_back_to_content(target):
    contextual = row_for("code_chunks")
    raw = {**contextual, "context_text": ""}
    await rebuild_batch("code_chunks", [contextual, raw], target)
    target.embed_many.assert_awaited_once_with(["contextual source", "raw source"])


@pytest.mark.asyncio
async def test_a_knowledge_chunk_embeds_its_context_before_its_text_as_its_write_did(target):
    contextual = row_for("chunks")
    raw = {**contextual, "context": ""}
    await rebuild_batch("chunks", [contextual, raw], target)
    target.embed_many.assert_awaited_once_with(
        ["Handbook\nRefunds\n\nWithin 14 days.", "Within 14 days."]
    )


@pytest.mark.asyncio
async def test_a_titled_chunk_with_no_text_rebuilds_its_context_as_its_write_did(target):
    """`knowledge.add` of empty content stores one empty chunk embedded as its context."""
    row = {**row_for("chunks"), "content": "", "context": "T"}
    await rebuild_batch("chunks", [row], target)
    target.embed_many.assert_awaited_once_with(["T\n\n"])


@pytest.mark.asyncio
async def test_tombstones_never_send_deleted_content_to_provider(target):
    row = row_for("memory_embeddings")
    row["tombstoned_at"] = datetime(2025, 2, 1, tzinfo=UTC)
    result = (await rebuild_batch("memory_embeddings", [row], target)).rows[0]
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
    result = (await rebuild_batch("chunks", rows, target)).rows
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
    row = {
        **row_for("chunks"),
        "content": "",
        "context": "",
        "embedding": stored,
        "embedding_dim": 0,
    }
    result = (await rebuild_batch("chunks", [row], target)).rows[0]
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
    result = (await rebuild_batch("code_summaries", [row], target)).rows[0]
    assert result["embedding_model"] == "target-model"
    assert (result["embedding"], result["embedding_dim"], result["pagerank"]) == ([], 0, 0.1)
    target.embed_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_source_text_cannot_silently_discard_an_existing_vector(target):
    row = {**row_for("chunks"), "content": "", "context": ""}
    with pytest.raises(RebuildBatchError, match="source_text_missing"):
        await rebuild_batch("chunks", [row], target)
    target.embed_many.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["events", "embedding_cache", "code_chunks_prefix", "unknown"])
async def test_non_reembed_actions_cannot_accidentally_send_data(target, table):
    with pytest.raises(RebuildBatchError, match="unsupported_rebuild_table"):
        await rebuild_batch(table, [], target)
    target.embed_many.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", sorted(PASSAGE_TABLES))
async def test_a_passage_that_reads_as_a_credential_is_rebuilt_without_a_vector(table, target):
    # Embedded by an earlier build; its text holds a key.
    secret = {**row_for(table), TEXT_COLUMNS[table]: "key " + AWS_KEY_ID}
    result = (await rebuild_batch(table, [row_for(table), secret], target)).rows
    target.embed_many.assert_awaited_once_with(["source text"])
    assert [r["embedding_dim"] for r in result] == [2, 0]


@pytest.mark.asyncio
async def test_a_memory_row_is_rebuilt_whatever_its_text(target):
    # Only raw passages are held; a memory row's vector follows its sensitivity label.
    row = {**row_for("memory_embeddings"), "content_excerpt": "key " + AWS_KEY_ID}
    result = (await rebuild_batch("memory_embeddings", [row], target)).rows
    assert result[0]["embedding_dim"] == 2


def _without_vector(table: str, text: str, stored: list[float] | None = None) -> dict:
    """A row stored with no vector: empty, or the all-zero placeholder `stored`."""
    row = {**row_for(table), TEXT_COLUMNS[table]: text, "embedding_dim": 0}
    for surface in EMBEDDING_SURFACES:
        if surface.table == table:
            row[surface.column] = stored or []
    return row


def _stored_chunks(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> list[tuple]:
    """Answer the session read from `rows`, as loom.memory_chunks would; returns the
    `(org, namespace, session_id, first, last)` window each read asked for."""
    asked: list[tuple] = []

    async def read(org, namespace, session_id, first, last, *, ch=None):
        asked.append((org, namespace, session_id, first, last))
        return [
            r
            for r in rows
            if (r["org"], r["namespace"], r["session_id"]) == (org, namespace, session_id)
            and first <= r["chunk_index"] <= last
        ]

    monkeypatch.setattr(provider_rebuild_batch, "session_chunks", read)
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [[], [0.0, 0.0, 0.0]], ids=["empty", "placeholder"])
async def test_a_plain_memory_chunk_without_a_vector_between_plain_chunks_is_embedded(
    stored, target, monkeypatch
):
    # Emptied by something other than a write-path hold (a failed embed, a rebuild while
    # the org's model calls were off): its neighbours touch no key, so it is embedded.
    rows = [
        {**row_for("memory_chunks"), "chunk_index": 0, "content": "plain words before"},
        {**_without_vector("memory_chunks", "source text", stored), "chunk_index": 1},
        {**row_for("memory_chunks"), "chunk_index": 2, "content": "plain words after"},
    ]
    asked = _stored_chunks(monkeypatch, rows)
    result = (await rebuild_batch("memory_chunks", rows[1:2], target)).rows[0]
    cap = max(settings.memory_chunks_max_per_call, IMPORT_MAX_ITEMS)
    assert asked == [("private-org", "private-ns", "s", 0, 1 + cap)]
    target.embed_many.assert_awaited_once_with(["source text"])
    assert result["embedding_dim"] == 2


@pytest.mark.asyncio
async def test_memory_chunks_with_vectors_read_no_neighbours(target, monkeypatch):
    asked = _stored_chunks(monkeypatch, [])
    await rebuild_batch("memory_chunks", [row_for("memory_chunks")], target)
    assert asked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["conversation_embeddings", "session_summaries"])
@pytest.mark.parametrize("stored", [[], [0.0, 0.0, 0.0]], ids=["empty", "placeholder"])
async def test_a_plain_turn_or_digest_stored_without_a_vector_is_embedded(table, stored, target):
    # Only a failed embed leaves a plain turn or digest without a vector.
    row = {**_without_vector(table, "source text", stored), "embed_last_error": "timeout"}
    result = (await rebuild_batch(table, [row], target)).rows[0]
    target.embed_many.assert_awaited_once_with(["source text"])
    assert result["embedding_dim"] == 2


@pytest.mark.asyncio
async def test_memory_chunks_cut_through_a_key_stay_without_a_vector_on_separate_pages(
    target, monkeypatch
):
    from server.memory.app.chunk_projection import chunk_text_with_ranges
    from server.memory.domain.sensitivity import reads_high

    text = ("plain words here " * 60)[:784] + f" {OPENAI_KEY} " + "more plain words " * 60
    chunks, _, _ = chunk_text_with_ranges(text, max_chunks=64)
    contents = [c for c, _, _ in chunks]
    assert len(contents) == 3 and not reads_high(contents[0])  # only the key's head
    # Stored as the write path leaves them: the two halves without a vector.
    rows = [
        {**row_for("memory_chunks"), "chunk_index": i, "content": c}
        if i == 2
        else {**_without_vector("memory_chunks", c), "chunk_index": i}
        for i, c in enumerate(contents)
    ]
    _stored_chunks(monkeypatch, rows)
    first_page = (await rebuild_batch("memory_chunks", rows[:1], target)).rows
    second_page = (await rebuild_batch("memory_chunks", rows[1:], target)).rows
    assert [r["embedding_dim"] for r in first_page + second_page] == [0, 0, 2]
    target.embed_many.assert_awaited_once_with([contents[2]])


@pytest.mark.asyncio
async def test_a_private_key_cut_into_many_chunks_stays_without_a_vector_on_every_page(
    target, monkeypatch
):
    from server.memory.app.chunk_projection import chunk_text_with_ranges
    from server.memory.domain.sensitivity import joined_overlaps_high

    # A 4096-bit RSA key's size in PEM form: 51 lines of 64 base64 characters.
    lines = [(f"Rt{n:02d}" + "Mq5Lp8Cd") * 5 + f"Rt{n:02d}" for n in range(51)]
    block = "\n".join(
        ["-----BEGIN RSA " + "PRIVATE KEY-----", *lines, "-----END RSA " + "PRIVATE KEY-----"]
    )
    text = "plain words here " * 50 + block + " more plain words" * 50
    chunks, _, _ = chunk_text_with_ranges(text, max_chunks=64)
    contents = [c for c, _, _ in chunks]
    held = joined_overlaps_high([("s", i, c) for i, c in enumerate(contents)])
    covers = [i for i, hit in enumerate(held) if hit]
    assert len([i for i in covers if not any(m in contents[i] for m in ("BEGIN", "END"))]) >= 2
    # Stored as the write path leaves them: every chunk the key covers without a vector.
    rows = [
        {**_without_vector("memory_chunks", c), "chunk_index": i}
        if i in covers
        else {**row_for("memory_chunks"), "chunk_index": i, "content": c}
        for i, c in enumerate(contents)
    ]
    _stored_chunks(monkeypatch, rows)
    staged = []
    for i in range(len(rows)):
        staged += (await rebuild_batch("memory_chunks", rows[i : i + 1], target)).rows
    assert [i for i, r in enumerate(staged) if r["embedding_dim"] == 0] == covers
    sent = [text for call in target.embed_many.await_args_list for text in call.args[0]]
    assert not any(line in text for text in sent for line in lines)


@pytest.mark.asyncio
async def test_a_long_session_of_plain_chunks_without_vectors_is_read_once_per_page(
    target, monkeypatch
):
    # An earlier rebuild left every chunk empty while the org's model calls were off.
    rows = [
        {**_without_vector("memory_chunks", f"plain words number {i} " * 30), "chunk_index": i}
        for i in range(300)
    ]
    asked = _stored_chunks(monkeypatch, rows)
    passes = []
    original = provider_rebuild_batch.joined_overlaps_high

    def counted(chunks):
        passes.append(len(chunks))
        return original(chunks)

    monkeypatch.setattr(provider_rebuild_batch, "joined_overlaps_high", counted)
    result = (await rebuild_batch("memory_chunks", rows[100:164], target)).rows
    assert len(asked) == 1 and len(passes) == 1
    assert [r["embedding_dim"] for r in result] == [2] * 64
