"""An empty chunk text is never sent to the embedder: providers reject it
(`input cannot be an empty string`), and one in a batch sent the whole
batch down the per-text path on every pass. The rest of the batch embeds
in one call, and the empty row is stamped failed at once."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.workers import embed_recovery as w


class _Embedder:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        if any(not t for t in texts):
            raise RuntimeError("400 input cannot be an empty string")
        return [[float(len(t))] for t in texts]


@pytest.mark.asyncio
async def test_empty_texts_are_not_sent_and_the_rest_embed_in_one_call() -> None:
    emb = _Embedder()
    vectors, failures = await w._vectors_for(emb, ["def f(): pass", "", "   ", "class A: ..."])
    assert emb.batches == [["def f(): pass", "class A: ..."]]
    assert vectors == {0: [13.0], 3: [12.0]}
    assert failures == {1: w.EMPTY_TEXT, 2: w.EMPTY_TEXT}


@pytest.mark.asyncio
async def test_a_batch_of_only_empty_texts_calls_nothing() -> None:
    emb = _Embedder()
    vectors, failures = await w._vectors_for(emb, ["", " "])
    assert emb.batches == [] and vectors == {}
    assert set(failures) == {0, 1}


class _Repo:
    def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
        return dict(live)


def test_an_empty_row_is_stamped_failed_on_its_first_attempt() -> None:
    result = w.CodeEmbedRecoveryResult()
    out: list[dict[str, Any]] = []
    group = [{"embed_attempts": 0}, {"embed_attempts": 0}]
    embedded = w._stage_replacements(
        _Repo(), "code_chunks", group, {0: [1.0]}, {1: w.EMPTY_TEXT}, 5, result, out
    )
    assert embedded == 1
    assert out[0]["embedding"] == [1.0] and out[0]["embed_failed_at"] is None
    assert out[1]["embed_attempts"] == 1 and out[1]["embed_failed_at"] is not None
    assert out[1]["embed_last_error"] == w.EMPTY_TEXT
    assert result.poisoned == 1
