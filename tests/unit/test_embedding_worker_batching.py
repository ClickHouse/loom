"""`_embed_batch`: batch the happy path, isolate failures per row.

This worker's value is its per-row failure accounting — attempts bumped, poison
detected, a retry stub re-inserted carrying that row's own error. `embed_many`
raises for the whole batch: a naive swap would fail 100 rows for one bad input
and lose every individual error. These tests pin the
behaviour that makes batching safe.
"""

from __future__ import annotations

import pytest

from server.memory.workers.embedding import _embed_batch


class _Embedder:
    """Records every `embed_many` call, so batching and per-row isolation are
    observable. `embed` is the query path; a stored vector must never use it."""

    model_name = "fake-test"
    dim = 3

    def __init__(
        self,
        *,
        many_raises: bool = False,
        many_returns: int | None = None,
        fail_texts: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[list[str]] = []
        self._many_raises = many_raises
        self._many_returns = many_returns
        self._fail_texts = fail_texts

    async def embed(self, text: str) -> list[float]:
        raise AssertionError("a stored vector came from the query path")

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if len(texts) > 1 and self._many_raises:
            raise RuntimeError("provider 500 on the whole batch")
        if any(t in self._fail_texts for t in texts):
            raise RuntimeError(f"boom on {texts}")
        n = self._many_returns if len(texts) > 1 and self._many_returns is not None else len(texts)
        return [[1.0, 2.0, 3.0] for _ in range(n)]


@pytest.mark.asyncio
async def test_happy_path_makes_one_batched_call() -> None:
    """One request per batch, not one per row: the API takes 100 inputs per
    request, and one call per row costs ~240 ms each."""
    e = _Embedder()
    out = await _embed_batch(e, [f"t{i}" for i in range(100)])
    assert len(e.calls) == 1 and len(e.calls[0]) == 100
    assert len(out) == 100
    assert all(v is not None and err == "" for v, err in out)


@pytest.mark.asyncio
async def test_batch_failure_isolates_the_bad_row() -> None:
    """One poison input must not fail its neighbours, and the failure keeps
    its own message (what the attempts/poison accounting writes into
    `embed_last_error`)."""
    e = _Embedder(many_raises=True, fail_texts=("bad",))
    out = await _embed_batch(e, ["ok1", "bad", "ok2"])
    assert e.calls == [["ok1", "bad", "ok2"], ["ok1"], ["bad"], ["ok2"]]
    assert out[0][0] is not None and out[0][1] == ""
    assert out[1][0] is None and "boom on" in out[1][1]
    assert out[2][0] is not None and out[2][1] == ""


@pytest.mark.asyncio
async def test_length_mismatch_is_refused_not_zipped() -> None:
    """A short batch zipped onto the rows would attach one memory's vector to
    another with no error anywhere; a mismatch falls back to per-row embeds."""
    e = _Embedder(many_returns=2)
    out = await _embed_batch(e, ["a", "b", "c"])
    assert e.calls == [["a", "b", "c"], ["a"], ["b"], ["c"]]
    assert len(out) == 3
    assert all(v is not None for v, _ in out)


@pytest.mark.asyncio
async def test_single_row_is_a_batch_of_one_over_the_raw_text() -> None:
    """The most common recovery (one memory whose inline embed failed) stores
    the raw text's vector, as memory.set does; `embed` would normalise it."""
    e = _Embedder()
    out = await _embed_batch(e, ["only  one\n"])
    assert e.calls == [["only  one\n"]]
    assert out[0][0] is not None


@pytest.mark.asyncio
async def test_empty_input_makes_no_calls() -> None:
    e = _Embedder()
    assert await _embed_batch(e, []) == []
    assert e.calls == []


@pytest.mark.asyncio
async def test_result_order_matches_input_order() -> None:
    """Callers `zip(rows, results, strict=True)`, so order is load-bearing."""
    e = _Embedder(many_raises=True, fail_texts=("b", "d"))
    out = await _embed_batch(e, ["a", "b", "c", "d"])
    assert [v is None for v, _ in out] == [False, True, False, True]


def test_reconciliation_stub_carries_the_embedder_model() -> None:
    """The stub's `embedding_model` must match what the embed will write.

    `memory_embeddings` is ReplacingMergeTree ORDER BY
    (org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key)
    — `embedding_model` is part of the DEDUP KEY. A reconciliation stub written with
    a different model than the embed produces is a different row, so the embedded row
    never replaces it, the `embedding_dim = 0` row survives forever, and
    `find_embedding_stubs` re-picks it on every pass.

    A stub with `embedding_model=""` would loop exactly that way, re-embedding
    the same memories on every pass until the orphan rows are deleted. Matching
    the model is what makes convergence structural rather than accidental.

    Asserted structurally because the loop only manifests against a real
    ReplacingMergeTree over several passes — no unit test can observe it, but this
    catches the one-token mistake that causes it.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "server" / "memory" / "workers" / "embedding.py"
    ).read_text()
    block = src.split("orphans = await memory_repo.find_memories_without_embedding_rows", 1)
    assert len(block) == 2, "reconciliation pass is missing"
    stub_build = block[1].split("find_embedding_stubs", 1)[0]
    assert "embedding_model=embedder.model_name" in stub_build, (
        "the reconciliation stub must carry the embedder's model name; an empty or "
        "mismatched model makes the row unreplaceable and re-embedded forever"
    )
    assert 'embedding_model=""' not in stub_build


def test_embedder_is_available_before_reconciliation() -> None:
    """Ordering matters: the stub needs the model name before it is built.

    `embedder = get_embedder()` must be bound before the stub drain: if
    reconciliation defaulted the model to "" instead, it would lint clean and
    loop forever.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "server" / "memory" / "workers" / "embedding.py"
    ).read_text()
    assert src.index("embedder = get_embedder()") < src.index(
        "find_memories_without_embedding_rows"
    ), "the embedder must be acquired before the reconciliation block uses it"
