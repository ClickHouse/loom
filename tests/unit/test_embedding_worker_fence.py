"""The embedding worker's replacement row must never outrank a write that
happened after the stub was read.

memory_embeddings is ReplacingMergeTree(version). With a fresh write-time
version, a memory.set that rewrote the memory between the stub SELECT and the
worker's INSERT would lose under FINAL: the memory would stay searchable by
stale text, and the new text would never be embedded (its stub out-versioned).
The fence writes stub_version + 1: newer than the stub, older than any
later write.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.workers import embedding as w
from server.memory.workers.embedding import EmbeddingWorkerConfig, _carry_signals, _fenced_version


def test_fenced_version_is_stub_plus_one_or_default() -> None:
    assert _fenced_version({"version": 1_700_000_000_000_000_000}) == 1_700_000_000_000_000_001
    assert _fenced_version({"version": "42"}) == 43
    assert _fenced_version({}) is None  # legacy stub without a version → default
    assert _fenced_version({"version": 0}) is None
    assert _fenced_version({"version": "junk"}) is None


def _stub(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "scope": "private",
        "content_excerpt": "v1 text",
        "embedding_model": "fake-test",
        "created_at": None,
        "embed_attempts": 0,
        "version": 1_000,
        "entities": [],
        "tags": [],
    }
    row.update(over)
    return row


def test_carry_signals_carries_the_fenced_version() -> None:
    assert _carry_signals(_stub())["version"] == 1_001
    assert _carry_signals(_stub(version=None))["version"] is None


class _Embedder:
    model_name = "fake-test"
    dim = 3

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def embed_many(self, texts):
        if self.fail:
            raise RuntimeError("provider down")
        return [[1.0, 2.0, 3.0] for _ in texts]

    async def embed(self, text):
        if self.fail:
            raise RuntimeError("provider down")
        return [1.0, 2.0, 3.0]


@pytest.fixture
def worker_env(monkeypatch):
    from server.memory import store as memory_repo

    inserted: list[list[dict[str, Any]]] = []
    stubs: list[dict[str, Any]] = [_stub()]

    async def find_stubs(**kw):
        return [dict(s) for s in stubs]

    async def insert_rows(rows, *a, **kw):
        inserted.append(list(rows))

    async def none_missing(**kw):
        return []

    async def no_refresh(rows):
        return None

    monkeypatch.setattr(memory_repo, "find_embedding_stubs", find_stubs)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", insert_rows)
    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", none_missing)
    monkeypatch.setattr(w, "_refresh_entities", no_refresh)
    env = {"inserted": inserted, "stubs": stubs, "embedder": _Embedder()}
    monkeypatch.setattr(w, "get_embedder", lambda: env["embedder"])
    return env


@pytest.mark.asyncio
async def test_replacement_row_is_versioned_just_above_the_stub(worker_env) -> None:
    result = await w.run_pass(EmbeddingWorkerConfig(batch_size=10))
    assert result.embedded_count == 1
    replacements = [r for batch in worker_env["inserted"] for r in batch if r["embedding_dim"] == 3]
    assert len(replacements) == 1
    row = replacements[0]
    assert row["version"] == 1_001  # stub + 1, not now()
    assert row["content_excerpt"] == "v1 text" and row["memory_key"] == "k"


@pytest.mark.asyncio
async def test_retry_stub_is_versioned_just_above_the_stub_too(worker_env) -> None:
    worker_env["embedder"].fail = True
    result = await w.run_pass(EmbeddingWorkerConfig(batch_size=10, max_attempts=5))
    assert result.failed_count == 1
    retries = [r for batch in worker_env["inserted"] for r in batch if r["embedding_dim"] == 0]
    assert len(retries) == 1 and retries[0]["version"] == 1_001
    assert retries[0]["embed_attempts"] == 1


@pytest.mark.asyncio
async def test_legacy_stub_without_version_still_gets_a_version(worker_env) -> None:
    worker_env["stubs"][0]["version"] = None
    await w.run_pass(EmbeddingWorkerConfig(batch_size=10))
    row = [r for batch in worker_env["inserted"] for r in batch][0]
    assert isinstance(row["version"], int) and row["version"] > 1_000_000


@pytest.mark.asyncio
async def test_a_stub_from_before_a_model_change_is_embedded_and_retired(worker_env) -> None:
    """embedding_model is in the sort key, so the new model's row does not
    replace an old model's stub; without a tombstone the stub would be
    re-picked (and re-embedded) on every pass, forever."""
    worker_env["stubs"][0]["embedding_model"] = "text-embedding-3-small"
    await w.run_pass(EmbeddingWorkerConfig(batch_size=10))
    rows = [r for batch in worker_env["inserted"] for r in batch]
    new = [r for r in rows if r["embedding_model"] == "fake-test"]
    old = [r for r in rows if r["embedding_model"] == "text-embedding-3-small"]
    assert len(new) == 1 and new[0]["embedding_dim"] == 3 and new[0]["tombstoned_at"] is None
    assert len(old) == 1 and old[0]["embedding_dim"] == 0 and old[0]["tombstoned_at"] is not None
    assert old[0]["version"] == 1_001  # replaces exactly the stub it retires


@pytest.mark.asyncio
async def test_a_stub_of_the_served_model_gets_no_tombstone(worker_env) -> None:
    await w.run_pass(EmbeddingWorkerConfig(batch_size=10))
    rows = [r for batch in worker_env["inserted"] for r in batch]
    assert all(r["tombstoned_at"] is None for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("org", ["", "-", "*"])
async def test_a_stub_naming_no_customer_is_given_up_on_without_a_model_call(
    worker_env, monkeypatch, org: str
) -> None:
    from server.llm import customer
    from server.observability import genai
    from server.settings import settings

    monkeypatch.setattr(settings, "inference_customer_header", "X-Customer")
    genai._identity.set(None)
    worker_env["stubs"][0]["org"] = org
    worker_env["embedder"].fail = True  # a call would be a provider failure
    result = await w.run_pass(EmbeddingWorkerConfig(batch_size=10, max_attempts=5))
    assert result.poisoned_count == 1 and result.failed_count == 0
    (retry,) = [r for batch in worker_env["inserted"] for r in batch if r["embedding_dim"] == 0]
    assert retry["embed_last_error"] == customer.NO_CUSTOMER
    assert retry["embed_attempts"] == 1 and retry["embed_failed_at"] is not None
