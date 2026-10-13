"""Async embedding worker — fills stub rows with computed embeddings."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from server.core.vectors.placeholders import placeholder  # noqa: E402
from server.memory.workers.embedding import (  # noqa: E402
    EmbeddingWorkerConfig,
    run_pass,
)
from server.settings import settings  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

# A stub's vectors: the zero placeholder at the served width, as async writes store it.
PLACEHOLDER = placeholder(settings.embedding_dim)

ORG = unique_org("embwk")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


@pytest.fixture(autouse=True)
async def _ch_connected():
    """Function-scoped — see test_auto_promote. pytest-asyncio's per-test
    loop closes the async CH singleton's connection under module
    scoping.

    Also resets the embedder cache: OpenAIEmbedder holds a
    httpx.AsyncClient bound to the test's event loop; without reset
    the second test reuses a closed client and the worker's
    embedder.embed() raises 'Event loop is closed', which
    embedding_worker catches and tags as 'retry' — leaving the stub
    unfilled and the search-after assertion failing."""
    from server.core.ch import get_client
    from server.embedding import embedder as _embedder_mod

    _embedder_mod.reset()
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


def _embedding_dim(ch_client, ns: str, memory_key: str) -> int:
    """Return the LARGEST embedding_dim seen across all rows for this
    (org, ns, memory_key) tuple.

    The stub inserts under `embedding_model='fake-ngram-1024'` (the dev
    value); the worker writes the replacement under whatever the
    live embedder reports (`text-embedding-3-small` in production).
    Because `embedding_model` is part of ORDER BY for
    loom.memory_embeddings, those two rows DON'T collapse under FINAL.
    Returning the max means "did the worker fill ANY row" rather than
    "did the stub get rewritten in place."
    """
    rows = ch_client.query(
        """
        SELECT max(embedding_dim) FROM loom.memory_embeddings FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key = {k:String}
        """,
        parameters={"org": ORG, "ns": ns, "k": memory_key},
    ).result_rows
    return int(rows[0][0]) if rows and rows[0][0] is not None else -1


@pytest.mark.asyncio
async def test_stub_inserted_directly_gets_embedded(
    control_plane_url: str,
    ch_client,
) -> None:
    """Insert a stub embedding row directly (simulating what the async-mode
    memory.set writes), then run the worker, verify it's filled in."""
    ns = f"emb-{uuid.uuid4().hex[:6]}"
    await _seed_memory_row(ns, "pending_k", "clickhouse columnar speed")
    ch_client.command(
        """
        INSERT INTO loom.memory_embeddings
          (org, namespace, agent, user_id, session_id, memory_key,
           memory_type, scope, content_excerpt, embedding_model,
           embedding_dim, embedding, memory_key_embedding, tombstoned_at, created_at)
        VALUES ({org:String}, {ns:String}, 'w', '-', '-', 'pending_k',
                'semantic', 'private', 'clickhouse columnar speed',
                'fake-ngram-1024', 0, {ph:Array(BFloat16)}, {ph:Array(BFloat16)}, NULL,
                now() - INTERVAL 1 MINUTE)
        """,
        parameters={"org": ORG, "ns": ns, "ph": PLACEHOLDER},
    )
    assert _embedding_dim(ch_client, ns, "pending_k") == 0  # stub

    result = await run_pass(
        EmbeddingWorkerConfig(
            org_filter=ORG,
            namespace_filter=ns,
        )
    )
    # The CI control plane boots with LOOM_BACKGROUND_WORKERS=off: nothing else
    # drains stubs, so THIS pass is the one that embeds the row and the count
    # is exact.
    assert result.embedded_count >= 1, f"the pass embedded nothing: {result!r}"

    # CH Cloud ReplacingMergeTree(created_at) FINAL needs a beat after
    # the worker writes the replacement row before the read collapses
    # to the populated version. Spin until embedding_dim > 0 or 5s.
    import time as _time

    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if _embedding_dim(ch_client, ns, "pending_k") > 0:
            break
        await asyncio.sleep(0.1)
    assert _embedding_dim(ch_client, ns, "pending_k") > 0


async def _seed_memory_row(ns: str, key: str, value: str) -> None:
    """The memory row an async-mode memory.set writes beside its stub: the worker
    fills only stubs whose head may keep a vector, and a stub with no head is an
    orphan."""
    import hashlib
    import json
    from datetime import UTC, datetime

    from server.core.ch import get_client
    from server.memory import store as M

    ch = get_client()
    await ch.connect()  # the shared client the worker pass uses next: left connected
    row = M.build_first_version_row(
        {
            "org": ORG,
            "namespace": ns,
            "agent": "w",
            "user_id": "-",
            "session_id": "-",
            "memory_key": key,
            "memory_type": "semantic",
            "value": json.dumps(value),
            "value_json": "{}",
            "content_sha256": hashlib.sha256(value.encode()).hexdigest(),
            "confidence": 1.0,
            "actor": "process:itest",
            "source_class": "asserted",
            "version": M.next_version(),
        },
        now=datetime.now(UTC).replace(tzinfo=None),
    )
    await M.insert_memory_versions([row], ch=ch)


@pytest.mark.asyncio
async def test_stub_invisible_to_search_until_embedded(
    control_plane_url: str,
    ch_client,
) -> None:
    """memory.search filter `embedding_dim > 0` hides stubs.

    With LOOM_BACKGROUND_WORKERS=off on the CI server no worker tick races the
    `before == []` read: the stub stays a stub until this test's own `run_pass`.
    """
    from loom import Agent

    ns = f"emb-srch-{uuid.uuid4().hex[:6]}"
    await _seed_memory_row(ns, "pending", "distinctive query content here")
    ch_client.command(
        """
        INSERT INTO loom.memory_embeddings
          (org, namespace, agent, user_id, session_id, memory_key,
           memory_type, scope, content_excerpt, embedding_model,
           embedding_dim, embedding, memory_key_embedding, tombstoned_at, created_at)
        VALUES ({org:String}, {ns:String}, 'w', '-', '-', 'pending',
                'semantic', 'private', 'distinctive query content here',
                'fake-ngram-1024', 0, {ph:Array(BFloat16)}, {ph:Array(BFloat16)}, NULL, now())
        """,
        parameters={"org": ORG, "ns": ns, "ph": PLACEHOLDER},
    )

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        # Pre-worker: no hits because embedding_dim=0 filtered out
        before = await a.memory.search("distinctive query content")
        assert before == []

    await run_pass(
        EmbeddingWorkerConfig(
            org_filter=ORG,
            namespace_filter=ns,
        )
    )

    # Spin on memory.search until the worker's replacement row collapses
    # via FINAL — ReplacingMergeTree visibility lag on Cloud, ~100-500ms.
    found = False
    for _ in range(50):
        async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
            after = await a.memory.search("distinctive query content")
        if any(h.memory_key == "pending" for h in after):
            found = True
            break
        await asyncio.sleep(0.1)
    assert found, "embedded stub never surfaced in memory.search within 5s"


@pytest.mark.asyncio
async def test_empty_pending_queue_is_safe() -> None:
    """run_pass with no pending stubs returns 0, doesn't error."""
    result = await run_pass(
        EmbeddingWorkerConfig(
            org_filter=f"isolated-{uuid.uuid4().hex[:6]}",
        )
    )
    assert result.embedded_count == 0


@pytest.mark.asyncio
async def test_records_worker_status() -> None:
    from server.workers.status import get_registry

    reg = get_registry()
    reg.reset()
    await run_pass(
        EmbeddingWorkerConfig(
            org_filter=f"x-{uuid.uuid4().hex[:6]}",
        )
    )
    snap = reg.snapshot()
    assert "embedding_worker" in snap
    s = snap["embedding_worker"]
    assert s["last_run_at"] is not None
    assert "embedded_count" in s["last_outcome"]


@pytest.mark.asyncio
async def test_worker_replacement_never_outranks_a_later_write(
    control_plane_url: str,
    ch_client,
) -> None:
    """The fence: a memory rewritten between the worker's stub SELECT and its
    INSERT must win under FINAL. Simulated by handing the worker a stale
    snapshot of the stub after a newer row for the same key has landed."""
    from server.memory import store as memory_repo

    ns = f"emb-fence-{uuid.uuid4().hex[:6]}"
    model = "fake-ngram-1024"

    def _insert(text: str, version: int) -> None:
        ch_client.command(
            """
            INSERT INTO loom.memory_embeddings
              (org, namespace, agent, user_id, session_id, memory_key,
               memory_type, scope, content_excerpt, embedding_model,
               embedding_dim, embedding, memory_key_embedding, tombstoned_at, created_at,
               version)
            VALUES ({org:String}, {ns:String}, 'w', '-', '-', 'fenced_k',
                    'semantic', 'private', {text:String}, {model:String},
                    0, {ph:Array(BFloat16)}, {ph:Array(BFloat16)}, NULL,
                    now() - INTERVAL 1 MINUTE, {v:UInt64})
            """,
            parameters={
                "org": ORG,
                "ns": ns,
                "text": text,
                "model": model,
                "v": version,
                "ph": PLACEHOLDER,
            },
        )

    _insert("v1 text", 1_000_000)
    # The worker reads the stub now...
    snapshot = await memory_repo.find_embedding_stubs(
        batch_size=10, max_attempts=5, org_filter=ORG, namespace_filter=ns
    )
    assert [r["content_excerpt"] for r in snapshot] == ["v1 text"]
    # ...and a memory.set rewrites the memory before the worker writes.
    _insert("v2 text", 2_000_000)

    async def _stale(**kw):
        return [dict(r) for r in snapshot]

    original = memory_repo.find_embedding_stubs
    memory_repo.find_embedding_stubs = _stale  # type: ignore[assignment]
    try:
        await run_pass(EmbeddingWorkerConfig(org_filter=ORG, namespace_filter=ns))
    finally:
        memory_repo.find_embedding_stubs = original  # type: ignore[assignment]

    rows = ch_client.query(
        """
        SELECT content_excerpt, embedding_dim, version FROM loom.memory_embeddings FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_key = 'fenced_k'
          AND embedding_model = {model:String}
        """,
        parameters={"org": ORG, "ns": ns, "model": model},
    ).result_rows
    # Only the embedder the control plane runs may differ in model name; with
    # the fake embedder the replacement lands under the same key and must lose.
    assert rows, "the row vanished"
    text, dim, version = rows[0]
    assert text == "v2 text", f"stale re-embed won: {rows}"
    # The fence's invariant is that the STALE replacement (v1 text at
    # 1_000_001) never outranks the later write. With no other worker on the
    # table (the CI server runs LOOM_BACKGROUND_WORKERS=off)
    # the v2 stub is untouched and the surviving version is exactly its own.
    assert int(version) == 2_000_000, f"stale re-embed won or a later write was outranked: {rows}"
