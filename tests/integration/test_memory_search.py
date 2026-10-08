"""memory.search end-to-end.

Verifies:
  - memory.set on searchable types (semantic/procedural/preference) auto-embeds
  - memory.set on episodic does NOT embed
  - memory.search returns ranked matches by cosine similarity
  - memory_type filter narrows by type
  - user_id filter scopes to a specific user (for preference recall)
  - per-agent isolation (agent_a's memory doesn't appear under agent_b)
  - agent-pipeline-shape: "what did I learn about pattern X" recall
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402

# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


@pytest.mark.asyncio
async def test_semantic_memory_set_writes_embedding(agent, ch_client) -> None:
    """memory.set on a searchable type lands a row in memory_embeddings.
    Wrapped in `eventually` because the embedding insert is
    async_insert=1 — see tests/integration/_eventually.py.
    """
    await agent.memory.set(
        "lesson-learned-1",
        {"pattern": "retry on 5xx", "context": "QueryProcessor"},
        memory_type="semantic",
    )

    async def count_embeddings() -> int:
        rows = ch_client.query(
            "SELECT count() FROM loom.memory_embeddings WHERE namespace = {ns:String}",
            parameters={"ns": agent.namespace},
        ).result_rows
        return int(rows[0][0])

    n = await eventually(count_embeddings, description="embedding row to be flushed")
    assert n >= 1


@pytest.mark.asyncio
async def test_episodic_memory_set_does_not_embed(agent, ch_client) -> None:
    """Episodic memory is session-scoped; not embedded → search can't find it."""
    await agent.memory.set(
        "session-start",
        {"timestamp": "2026-05-13T17:00:00"},
        memory_type="episodic",
    )
    rows = ch_client.query(
        "SELECT count() FROM loom.memory_embeddings WHERE namespace = {ns:String}",
        parameters={"ns": agent.namespace},
    ).result_rows
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_memory_search_returns_relevant_match(agent) -> None:
    """agent-pipeline-shape: store a learning, recall it by querying with similar content."""
    await agent.memory.set(
        "pattern_x_lessons",
        {
            "pattern": "NULL pointer in retry loop",
            "fix": "guard with explicit nil check",
        },
        memory_type="semantic",
    )

    async def search_for_hit():
        hits = await agent.memory.search(
            "NULL pointer in retry loop",
            memory_type="semantic",
            top_k=5,
        )
        return hits if hits else None

    hits = await eventually(search_for_hit, description="semantic search to surface the row")
    assert hits[0].memory_key == "pattern_x_lessons"
    assert hits[0].similarity > 0.5  # paraphrase territory for fake-ngram embedder


@pytest.mark.asyncio
async def test_memory_search_orders_by_relevance(agent) -> None:
    """Most-similar memory ranks first."""
    await agent.memory.set(
        "memory_a",
        {"text": "Memory leak in materialized view background merge"},
        memory_type="semantic",
    )
    await agent.memory.set(
        "memory_b",
        {"text": "Authentication bypass in REST handler under specific TLS"},
        memory_type="semantic",
    )

    async def both_visible():
        hits = await agent.memory.search(
            "materialized view background merge memory leak",
            memory_type="semantic",
            top_k=2,
        )
        return hits if len(hits) == 2 else None

    hits = await eventually(both_visible, description="both embedding rows to be flushed")
    assert hits[0].memory_key == "memory_a"
    assert hits[0].distance < hits[1].distance


@pytest.mark.asyncio
async def test_memory_search_filters_by_type(agent) -> None:
    """memory_type filter narrows results to that type."""
    common = {"text": "common payload phrase shared across all the memory types"}
    await agent.memory.set("sem_only", common, memory_type="semantic")
    await agent.memory.set("proc_only", common, memory_type="procedural")
    await agent.memory.set("pref_only", common, memory_type="preference")

    # Poll until the write is searchable (async-insert/index visibility race).
    async def _sem():
        hits = await agent.memory.search(
            "common payload phrase shared", memory_type="semantic", top_k=10
        )
        keys = {h.memory_key for h in hits}
        return keys if "sem_only" in keys else None

    keys = await eventually(_sem, description="sem_only via type-filtered search")
    assert "sem_only" in keys
    assert "proc_only" not in keys
    assert "pref_only" not in keys


@pytest.mark.asyncio
async def test_memory_search_filters_by_user_id(agent) -> None:
    """user_id filter scopes preferences to a specific user."""
    await agent.memory.set(
        "prefers_json",
        True,
        memory_type="preference",
        user_id="alice",
    )
    await agent.memory.set(
        "prefers_yaml",
        True,
        memory_type="preference",
        user_id="bob",
    )

    # Poll until the write is searchable (async-insert/index visibility race).
    async def _alice():
        hits = await agent.memory.search(
            "prefers", memory_type="preference", user_id="alice", top_k=10
        )
        keys = {h.memory_key for h in hits}
        return keys if "prefers_json" in keys else None

    keys = await eventually(_alice, description="prefers_json via user_id-filtered search")
    assert "prefers_json" in keys
    assert "prefers_yaml" not in keys  # bob's pref must not leak into alice's filter


@pytest.mark.asyncio
async def test_memory_search_membership_reads_all_agents(detector_agent, validator_agent) -> None:
    """Membership is the read boundary: within ONE namespace a co-member reads
    another agent's rows by default (agent is authorship, not a read gate).
    Passing an explicit agent_id still narrows the search to that one author."""
    await detector_agent.memory.set(
        "detector_lesson",
        {"x": "detector-only content with unique phrase"},
        memory_type="semantic",
    )

    # Default (no agent) → membership read-all: the co-namespace validator
    # SEES the detector's row; isolation takes an explicit agent_id.
    async def _seen():
        hits = await validator_agent.memory.search(
            "detector-only content with unique phrase",
            memory_type="semantic",
            top_k=5,
        )
        keys = {h.memory_key for h in hits}
        return keys if "detector_lesson" in keys else None

    keys = await eventually(_seen, description="cross-agent read within a shared namespace")
    assert "detector_lesson" in keys

    # Explicit agent_id = validator's own agent → narrowed to its own rows,
    # so the detector's row is excluded (isolation on demand).
    hits = await validator_agent.memory.search(
        "detector-only content with unique phrase",
        memory_type="semantic",
        agent_id="validator",
        top_k=5,
    )
    assert "detector_lesson" not in {h.memory_key for h in hits}


@pytest.mark.asyncio
async def test_memory_search_returns_empty_when_nothing_set(agent) -> None:
    """Fresh namespace → empty search results."""
    hits = await agent.memory.search("anything", top_k=10)
    assert hits == []


# ============================================================
# Extra fixtures for the isolation test
# ============================================================


@pytest.fixture
async def detector_agent(control_plane_url: str):
    from loom import Agent

    ns = f"ms-{uuid.uuid4().hex[:8]}"
    async with Agent(
        org="clickhouse",
        namespace=ns,
        agent="detector",
        base_url=control_plane_url,
    ) as a:
        yield a


@pytest.fixture
async def validator_agent(detector_agent, control_plane_url: str):
    from loom import Agent

    async with Agent(
        org="clickhouse",
        namespace=detector_agent.namespace,
        agent="validator",
        base_url=control_plane_url,
    ) as a:
        yield a
