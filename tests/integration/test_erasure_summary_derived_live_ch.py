"""A user erasure removes what was derived from a consolidated summary of the user's
memories along with the summary: its version history, its verbatim chunks, and the
graph rows extracted from it, tombstoning or purging; the residue census counts the
same rows. A summary that folded none of the user's memories keeps all of them, and so
does a triple another user's memory also carries. In-process against a live
ClickHouse; skips without LOOM_CH_HOST."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.core.ch import get_client
from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_graph as graph_steps
from server.erasure.store import steps_memory as memory_steps
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

_MEMORY_COLS = ["org", "namespace", "agent", "user_id", "session_id", "memory_key"]
_MEMORY_COLS += ["memory_type", "value", "version", "tags", "memory_id"]
_SUMMARY_ID = "00000000-0000-4000-8000-000000000001"
_BOB_SUMMARY_ID = "00000000-0000-4000-8000-000000000002"
_VECTOR = [0.0] * 3072
_COUNTED = (
    "loom.memory_versions",
    "loom.memory_chunks",
    "loom.entity_triple_memories",
    "loom.entity_triple_rows",
    "loom.entity_vectors",
)
# The user saga's steps this covers, in saga order.
_STEPS = (
    ("memory.tombstone", memory_steps.tombstone_memory_by_user),
    ("memory_chunks.tombstone", memory_steps.tombstone_memory_chunks_by_user),
    ("entity_graph.delete", graph_steps.delete_entity_graph_by_user),
    ("entity_vectors.delete", graph_steps.delete_entity_vectors_by_user),
    ("graph_links.delete", graph_steps.delete_graph_links_by_user),
    ("consolidated.tombstone", graph_steps.tombstone_consolidated_dependents_by_user),
    ("memory_versions.delete", memory_steps.delete_memory_versions_by_user),
    ("memory.retire", graph_steps.retire_memory_by_user),
)


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    client = get_client()
    await client.connect()
    yield client
    await client.disconnect()


def _seed(ch_client: Any, org: str) -> None:
    def memory(user_id: str, key: str, value: str, tags: list[str], memory_id: str) -> None:
        row = [org, "ns", "a", user_id, "-", key, "fact", value, 1, tags, memory_id]
        ch_client.insert("loom.memory", [row], column_names=_MEMORY_COLS)

    memory("alice", "user_diet", "alice is vegetarian", [], "00000000-0000-4000-8000-00000000000a")
    memory("bob", "bob-diet", "bob eats fish", [], "00000000-0000-4000-8000-00000000000b")
    # under '-', as consolidation writes them; the view copies each into the history
    memory(
        "-",
        "topic-diets",
        "alice is vegetarian; bob eats fish",
        ["consolidated", "src:user_diet", "src:bob-diet"],
        _SUMMARY_ID,
    )
    memory("-", "topic-bob", "bob eats fish", ["consolidated", "src:bob-diet"], _BOB_SUMMARY_ID)
    chunk_cols = ["org", "namespace", "agent", "user_id", "session_id", "chunk_index"]
    chunk_cols += ["content", "content_sha256", "parent_memory_key", "embedding"]
    ch_client.insert(
        "loom.memory_chunks",
        [
            [
                org,
                "ns",
                "a",
                "-",
                "-",
                0,
                text,
                hashlib.sha256(text.encode()).hexdigest(),
                key,
                _VECTOR,
            ]
            for text, key in (
                ("alice is vegetarian", "topic-diets"),
                ("bob eats fish", "topic-bob"),
            )
        ],
        column_names=chunk_cols,
    )
    triples = [
        ("alice", "eats", "vegetables", "user_diet"),
        ("alice", "eats", "vegetables", "topic-diets"),
        ("diets", "differ", "by person", "topic-diets"),
        ("bob", "eats", "fish", "bob-diet"),
        ("bob", "eats", "fish", "topic-diets"),
    ]
    ch_client.insert(
        "loom.entity_triples",
        [[org, "ns", s, r, o] for s, r, o in sorted({t[:3] for t in triples})],
        column_names=["org", "namespace", "subject", "relation", "object"],
    )
    ch_client.insert(
        "loom.entity_triple_memories",
        [[org, "ns", *t] for t in triples],
        column_names=["org", "namespace", "subject", "relation", "object", "memory_key"],
    )
    ch_client.insert(
        "loom.entity_triple_rows",
        [
            [org, "ns", _SUMMARY_ID, "-", "diets", "differ", "by person"],
            [org, "ns", _BOB_SUMMARY_ID, "-", "bob", "eats", "fish"],
        ],
        column_names=["org", "namespace", "memory_id", "user_id", "subject", "relation"]
        + ["object"],
    )
    ch_client.insert(
        "loom.entity_vectors",
        [
            [org, "ns", "a", "-", "vegetarian", "topic-diets", _VECTOR],
            [org, "ns", "a", "-", "fish", "topic-bob", _VECTOR],
        ],
        column_names=["org", "namespace", "agent", "user_id", "entity_text", "memory_key"]
        + ["entity_embedding"],
    )


def _column(ch_client: Any, org: str, sql: str) -> list[str]:
    rows = ch_client.query(sql, parameters={"org": org}).result_rows
    return sorted(str(r[0]) for r in rows)


@pytest.mark.parametrize("purge", [False, True])
async def test_what_a_summary_of_the_users_memories_fed_leaves_with_it(
    ch_client, ch, purge: bool
) -> None:
    org = unique_org("ersumdv")
    _seed(ch_client, org)
    with erasure_mutations.purge_scope(purge), erasure_mutations.saga_scope(f"erasure-{org}"):
        before = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        assert {t: before[t] for t in _COUNTED} == {
            "loom.memory_versions": 2,  # alice's row and the summary's
            "loom.memory_chunks": 1,
            "loom.entity_triple_memories": 4,  # alice's link and the summary's three
            "loom.entity_triple_rows": 1,
            "loom.entity_vectors": 1,
        }
        for name, step in _STEPS:
            with erasure_mutations.step_scope(name):
                await step(org, "alice", ch=ch)
        after = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
    assert {t: after[t] for t in _COUNTED} == dict.fromkeys(_COUNTED, 0)

    where = " WHERE org = {org:String}"
    history = _column(ch_client, org, "SELECT memory_key FROM loom.memory_versions" + where)
    assert history == ["bob-diet", "topic-bob"]
    chunks = "SELECT parent_memory_key FROM loom.memory_chunks FINAL" + where
    assert _column(ch_client, org, chunks + " AND tombstoned_at IS NULL") == ["topic-bob"]
    triples = "SELECT subject FROM loom.entity_triples" + where
    assert _column(ch_client, org, triples) == ["bob"]
    links = "SELECT memory_key FROM loom.entity_triple_memories" + where
    assert _column(ch_client, org, links) == ["bob-diet"]
    rows = "SELECT toString(memory_id) FROM loom.entity_triple_rows" + where
    assert _column(ch_client, org, rows) == [_BOB_SUMMARY_ID]
    vectors = "SELECT memory_key FROM loom.entity_vectors" + where
    assert _column(ch_client, org, vectors) == ["topic-bob"]
