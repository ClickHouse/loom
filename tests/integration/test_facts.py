"""facts.list / facts.related — promoting `entity_triples` to a wire op.

The data populated here comes from caller-supplied `relations` on
`memory.bulk_set` (typed triple `(subject, verb, object)`) or from
the LLM extractor emitting them on `memory.set_from_messages`. The
read surface is a first-class query plane: get all facts about an
entity, walk neighbors, fetch source memories.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402


# A small helper: wait for the triples writer (async-y on the server
# side; populates entity_triples + entity_triple_memories after the
# memory row commits) to catch up before reading.
async def _wait_for_triple(agent, *, subject: str, timeout_s: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        out = await agent.facts.list(subject=subject, limit=10)
        if out["rows"]:
            return
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_facts_list_filters_by_subject(agent) -> None:
    """facts.list(subject=X) returns triples where subject=X."""
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "f1",
                "value": "Alice works at Acme.",
                "memory_type": "semantic",
                "relations": [("alice", "works_at", "clickhouse")],
            },
            {
                "memory_key": "f2",
                "value": "Alice lives in NYC.",
                "memory_type": "semantic",
                "relations": [("alice", "lives_in", "nyc")],
            },
            {
                "memory_key": "f3",
                "value": "Bob works at Acme.",
                "memory_type": "semantic",
                "relations": [("bob", "works_at", "clickhouse")],
            },
        ]
    )
    await _wait_for_triple(agent, subject="alice")

    out = await agent.facts.list(subject="alice")
    subjects = {r["subject"] for r in out["rows"]}
    assert subjects == {"alice"}, f"expected only alice; got {subjects}"
    relations = {r["relation"] for r in out["rows"]}
    # Verb canonicalization stems suffixes: works_at → work_at, lives_in → live_in
    assert "work_at" in relations
    assert "live_in" in relations


@pytest.mark.asyncio
async def test_facts_list_entity_filters_both_directions(agent) -> None:
    """facts.list(entity=X) returns triples where X is subject OR object."""
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "f1",
                "value": "Alice works at Acme.",
                "memory_type": "semantic",
                "relations": [("alice", "works_at", "clickhouse")],
            },
            {
                "memory_key": "f2",
                "value": "Acme employs Bob.",
                "memory_type": "semantic",
                "relations": [("clickhouse", "employs", "bob")],
            },
        ]
    )
    await _wait_for_triple(agent, subject="alice")

    out = await agent.facts.list(entity="clickhouse")
    # acme should show up as either subject or object
    triples = {(r["subject"], r["relation"], r["object"]) for r in out["rows"]}
    assert any(t[0] == "clickhouse" or t[2] == "clickhouse" for t in triples)
    assert ("alice", "work_at", "clickhouse") in triples
    assert ("clickhouse", "employ", "bob") in triples


@pytest.mark.asyncio
async def test_facts_list_relation_filter(agent) -> None:
    """facts.list(relation=X) narrows to one verb."""
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "f1",
                "value": "Alice works at Acme.",
                "memory_type": "semantic",
                "relations": [("alice", "works_at", "clickhouse")],
            },
            {
                "memory_key": "f2",
                "value": "Bob works at Acme.",
                "memory_type": "semantic",
                "relations": [("bob", "works_at", "clickhouse")],
            },
            {
                "memory_key": "f3",
                "value": "Alice lives in NYC.",
                "memory_type": "semantic",
                "relations": [("alice", "lives_in", "nyc")],
            },
        ]
    )
    await _wait_for_triple(agent, subject="alice")

    out = await agent.facts.list(relation="work_at")
    relations = {r["relation"] for r in out["rows"]}
    assert relations == {"work_at"}
    objects = {r["object"] for r in out["rows"]}
    assert "clickhouse" in objects
    assert "nyc" not in objects  # filtered out by relation


@pytest.mark.asyncio
async def test_facts_list_include_sources(agent) -> None:
    """facts.list(include_sources=True) fans out to source memory_keys."""
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "alice_employer_a",
                "value": "Alice works at Acme.",
                "memory_type": "semantic",
                "relations": [("alice", "works_at", "clickhouse")],
            },
            {
                "memory_key": "alice_employer_b",
                "value": "Alice's job is at Acme.",
                "memory_type": "semantic",
                "relations": [("alice", "works_at", "clickhouse")],
            },
        ]
    )
    # Poll until BOTH sources have aggregated into the triple — _wait_for_triple
    # only waits for the triple to EXIST (one source projects before the second
    # aggregates into co_occur/source_keys), which races this assertion.
    want = {"alice_employer_a", "alice_employer_b"}

    async def _both_sources():
        out = await agent.facts.list(subject="alice", include_sources=True)
        t = next(
            (r for r in out["rows"] if r["relation"] == "work_at" and r["object"] == "clickhouse"),
            None,
        )
        if t and t.get("co_occur", 0) >= 2 and want <= set(t.get("source_keys") or []):
            return t
        return None

    target = await eventually(_both_sources, description="both sources aggregated into triple")
    assert target["co_occur"] >= 2
    assert set(target["source_keys"]) >= want


@pytest.mark.asyncio
async def test_facts_related_returns_neighbors(agent) -> None:
    """facts.related(entity=X) returns co-occurring entities."""
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "f1",
                "value": "Alice works at Acme.",
                "memory_type": "semantic",
                "relations": [("alice", "works_at", "clickhouse")],
            },
            {
                "memory_key": "f2",
                "value": "Alice lives in NYC.",
                "memory_type": "semantic",
                "relations": [("alice", "lives_in", "nyc")],
            },
            {
                "memory_key": "f3",
                "value": "Alice knows Bob.",
                "memory_type": "semantic",
                "relations": [("alice", "knows", "bob")],
            },
        ]
    )
    await _wait_for_triple(agent, subject="alice")

    out = await agent.facts.related(entity="alice", top_k=10)
    neighbors = {r["entity"] for r in out["related"]}
    assert "clickhouse" in neighbors
    assert "nyc" in neighbors
    assert "bob" in neighbors
    # Echoed
    assert out["entity"] == "alice"


@pytest.mark.asyncio
async def test_facts_related_role_classifies_direction(agent) -> None:
    """When `entity` is subject of triple A and object of triple B with
    the same neighbor, role should reflect both directions."""
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "f1",
                "value": "Alice works at Acme.",
                "memory_type": "semantic",
                "relations": [("alice", "works_at", "clickhouse")],
            },
            {
                "memory_key": "f2",
                "value": "Acme employs Alice.",
                "memory_type": "semantic",
                "relations": [("clickhouse", "employs", "alice")],
            },
        ]
    )
    await _wait_for_triple(agent, subject="alice")

    out = await agent.facts.related(entity="alice", top_k=10)
    # acme appears as object (from alice works_at clickhouse) AND as subject
    # (from clickhouse employs alice) → role="both"
    acme_neighbor = next((r for r in out["related"] if r["entity"] == "clickhouse"), None)
    assert acme_neighbor is not None
    assert acme_neighbor["role"] == "both"


@pytest.mark.asyncio
async def test_facts_list_empty_namespace_returns_empty(agent) -> None:
    """Calling facts.list on a namespace with no triples returns []."""
    out = await agent.facts.list(entity="nonexistent_entity")
    assert out["rows"] == []
    assert out["truncated"] is False
