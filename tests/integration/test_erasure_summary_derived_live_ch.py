"""A user erasure removes what was derived from a consolidated summary of the user's
memories along with the summary: its version history, its verbatim chunks and entity
vectors, and the graph rows extracted from it, tombstoning or purging; the residue
census counts the same rows. A summary that folded none of the user's memories keeps
all of them, a rebuilt summary keeps the versions that no longer name the user, and so
do a triple or an edge another user's memory also carries and another identity's rows
at the summary's key. In-process against a live ClickHouse; skips without
LOOM_CH_HOST."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from server.core.ch import get_client
from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_graph as graph_steps
from server.erasure.store import steps_memory as memory_steps
from server.memory.store.rows import build_embedding_row
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

_MEMORY_COLS = ["org", "namespace", "agent", "user_id", "session_id", "memory_key"]
_MEMORY_COLS += ["memory_type", "value", "version", "tags", "memory_id"]
_SUMMARY_ID = "00000000-0000-4000-8000-000000000001"
_BOB_SUMMARY_ID = "00000000-0000-4000-8000-000000000002"
_VECTOR = [0.0] * 3072
_COUNTED = {
    "loom.memory_versions": 3,  # alice's row, the summary's, the rebuilt one's first
    "loom.memory_chunks": 1,
    # alice's link, the summary's three, the rebuilt summary's from its old text
    "loom.entity_triple_memories": 5,
    "loom.entity_triple_rows": 1,
    "loom.entity_edge_memories": 3,  # alice's link and the summary's two
    "loom.entity_edge_rows": 1,
    "loom.entity_communities": 1,
    "loom.entity_vectors": 1,
}
# The user saga's steps this covers, in saga order.
_STEPS = (
    ("memory.tombstone", memory_steps.tombstone_memory_by_user),
    ("consolidated.tombstone", graph_steps.tombstone_consolidated_dependents_by_user),
    ("shared_embeddings.tombstone", memory_steps.tombstone_shared_embeddings_by_user),
    ("shared.tombstone", memory_steps.tombstone_shared_by_user),
    ("memory_chunks.tombstone", memory_steps.tombstone_memory_chunks_by_user),
    ("entity_graph.delete", graph_steps.delete_entity_graph_by_user),
    ("entity_communities.delete", graph_steps.delete_entity_communities_by_user),
    ("entity_vectors.delete", graph_steps.delete_entity_vectors_by_user),
    ("graph_links.delete", graph_steps.delete_graph_links_by_user),
    ("memory_versions.delete", memory_steps.delete_memory_versions_by_user),
    ("memory.retire", graph_steps.retire_memory_by_user),
)
_WHERE = " WHERE org = {org:String}"


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    client = get_client()
    await client.connect()
    yield client
    await client.disconnect()


def _insert(ch_client: Any, table: str, columns: list[str], rows: list[list[Any]]) -> None:
    ch_client.insert(table, rows, column_names=columns)


def _seed(ch_client: Any, org: str) -> None:
    def memory(user: str, key: str, value: str, tags: list[str], mid: str, v: int = 1) -> None:
        row = [org, "ns", "a", user, "-", key, "fact", value, v, tags, mid]
        _insert(ch_client, "loom.memory", _MEMORY_COLS, [row])

    memory("alice", "user_diet", "alice is vegetarian", [], "00000000-0000-4000-8000-00000000000a")
    memory("bob", "bob-diet", "bob eats fish", [], "00000000-0000-4000-8000-00000000000b")
    # another identity's memory at the summary's key: not the summary
    memory("carol", "topic-diets", "carol is vegan", [], "00000000-0000-4000-8000-00000000000c")
    # under '-', as consolidation writes them; the view copies each into the history
    tags = ["consolidated", "src:user_diet", "src:bob-diet"]
    memory("-", "topic-diets", "alice is vegetarian; bob eats fish", tags, _SUMMARY_ID)
    memory("-", "topic-bob", "bob eats fish", ["consolidated", "src:bob-diet"], _BOB_SUMMARY_ID)
    # a copy restored into ns2, where alice holds no key: same id, not hers there
    copy = [org, "ns2", "a", "-", "-", "topic-diets", "fact", "alice is vegetarian", 1, tags]
    _insert(ch_client, "loom.memory", _MEMORY_COLS, [[*copy, _SUMMARY_ID]])
    # rebuilt under the same key from bob's fact alone: the newer version stays
    rebuilt = "00000000-0000-4000-8000-000000000003"
    memory("-", "topic-rebuilt", "alice is vegetarian", ["consolidated", "src:user_diet"], rebuilt)
    memory("-", "topic-rebuilt", "bob eats fish", ["consolidated", "src:bob-diet"], rebuilt, v=2)
    chunks = [("-", "alice is vegetarian", "topic-diets"), ("-", "bob eats fish", "topic-bob")]
    chunks.append(("carol", "carol is vegan", "topic-diets"))
    _insert(
        ch_client,
        "loom.memory_chunks",
        ["org", "namespace", "agent", "user_id", "session_id", "chunk_index", "content"]
        + ["content_sha256", "parent_memory_key", "embedding"],
        [
            [org, "ns", "a", user, "-", 0, text, hashlib.sha256(text.encode()).hexdigest()]
            + [key, _VECTOR]
            for user, text, key in chunks
        ],
    )
    triples = [
        ("alice", "eats", "vegetables", "user_diet"),
        ("alice", "eats", "vegetables", "topic-diets"),
        ("diets", "differ", "by person", "topic-diets"),
        ("bob", "eats", "fish", "bob-diet"),
        ("bob", "eats", "fish", "topic-diets"),
        ("alice", "likes", "salad", "topic-rebuilt"),
    ]
    triple_cols = ["org", "namespace", "subject", "relation", "object"]
    _insert(
        ch_client,
        "loom.entity_triples",
        triple_cols,
        [[org, "ns", *t] for t in sorted({t[:3] for t in triples})],
    )
    _insert(
        ch_client,
        "loom.entity_triple_memories",
        [*triple_cols, "memory_key"],
        [[org, "ns", *t] for t in triples],
    )
    _insert(
        ch_client,
        "loom.entity_triple_rows",
        ["org", "namespace", "memory_id", "user_id", "subject", "relation", "object"],
        [
            [org, "ns", _SUMMARY_ID, "-", "diets", "differ", "by person"],
            [org, "ns", _BOB_SUMMARY_ID, "-", "bob", "eats", "fish"],
            [org, "ns2", _SUMMARY_ID, "-", "diets", "differ", "by person"],
        ],
    )
    edges = [
        ("alice", "vegetables", "user_diet"),
        ("alice", "vegetables", "topic-diets"),
        ("bob", "fish", "bob-diet"),
        ("bob", "fish", "topic-diets"),
    ]
    edge_cols = ["org", "namespace", "entity_a", "entity_b"]
    _insert(
        ch_client,
        "loom.entity_edges",
        edge_cols,
        [[org, "ns", *e] for e in sorted({e[:2] for e in edges})],
    )
    _insert(
        ch_client,
        "loom.entity_edge_memories",
        [*edge_cols, "memory_key"],
        [[org, "ns", *e] for e in edges],
    )
    _insert(
        ch_client,
        "loom.entity_edge_rows",
        ["org", "namespace", "memory_id", "user_id", "entity_a", "entity_b"],
        [
            [org, "ns", _SUMMARY_ID, "-", "alice", "vegetables"],
            [org, "ns", _BOB_SUMMARY_ID, "-", "bob", "fish"],
        ],
    )
    _insert(
        ch_client,
        "loom.entity_communities",
        ["org", "namespace", "entity", "community_id", "label"],
        [[org, "ns", "vegetables", 1, "vegetables"], [org, "ns", "fish", 2, "fish"]],
    )
    _insert(
        ch_client,
        "loom.entity_vectors",
        ["org", "namespace", "agent", "user_id", "entity_text", "memory_key", "entity_embedding"],
        [
            [org, "ns", "a", "-", "vegetarian", "topic-diets", _VECTOR],
            [org, "ns", "a", "-", "fish", "topic-bob", _VECTOR],
            [org, "ns", "a", "carol", "vegan", "topic-diets", _VECTOR],
        ],
    )


def _seed_shared(
    ch_client: Any, org: str, keys: tuple[str, ...] = ("topic-diets", "topic-bob")
) -> None:
    """Org-wide copies promoted from summaries, with their search rows."""
    _insert(
        ch_client,
        "loom.shared",
        ["org", "namespace", "memory_key", "value", "memory_type", "confidence"]
        + ["promoted_from", "promotion_event", "promoted_by", "reversible_until", "version"],
        [
            [org, "ns", key, "text", "fact", 1.0, [f"{org}/ns/a/-/-/{key}"], uuid.uuid4()]
            + ["explicit", datetime(2099, 1, 1), 1]
            for key in keys
        ],
    )
    for key in keys:
        vector = build_embedding_row(
            org=org,
            namespace="ns",
            agent="_shared",
            user_id="-",
            session_id="-",
            memory_key=key,
            scope="shared",
            content_excerpt="text",
            embedding=_VECTOR,
            memory_key_embedding=_VECTOR,
            embedding_dim=3072,
            version=1,
        )
        _insert(ch_client, "loom.memory_embeddings", list(vector), [list(vector.values())])


def _column(ch_client: Any, org: str, sql: str) -> list[str]:
    rows = ch_client.query(sql, parameters={"org": org}).result_rows
    return sorted(str(r[0]) for r in rows)


async def _run(steps: Any, org: str, ch: Any) -> None:
    for name, step in steps:
        with erasure_mutations.step_scope(name):
            await step(org, "alice", ch=ch)


@pytest.mark.parametrize("purge", [False, True])
async def test_what_a_summary_of_the_users_memories_fed_leaves_with_it(
    ch_client, ch, purge: bool
) -> None:
    org = unique_org("ersumdv")
    _seed(ch_client, org)
    _seed_shared(ch_client, org)
    with erasure_mutations.purge_scope(purge), erasure_mutations.saga_scope(f"erasure-{org}"):
        before = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        assert {t: before[t] for t in _COUNTED} == _COUNTED
        await _run(_STEPS, org, ch)
        after = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
    assert {t: after[t] for t in _COUNTED} == dict.fromkeys(_COUNTED, 0)

    history = "SELECT concat(user_id, ':', memory_key, ':', toString(version))"
    assert _column(ch_client, org, f"{history} FROM loom.memory_versions{_WHERE}") == [
        "-:topic-bob:1",
        "-:topic-diets:1",  # the copy in ns2
        "-:topic-rebuilt:2",
        "bob:bob-diet:1",
        "carol:topic-diets:1",
    ]
    chunks = f"SELECT concat(user_id, ':', parent_memory_key) FROM loom.memory_chunks FINAL{_WHERE}"
    live = _column(ch_client, org, chunks + " AND tombstoned_at IS NULL")
    assert live == ["-:topic-bob", "carol:topic-diets"]
    assert _column(ch_client, org, f"SELECT subject FROM loom.entity_triples{_WHERE}") == ["bob"]
    links = f"SELECT memory_key FROM loom.entity_triple_memories{_WHERE}"
    assert _column(ch_client, org, links) == ["bob-diet"]
    rows = (
        f"SELECT concat(namespace, ':', toString(memory_id)) FROM loom.entity_triple_rows{_WHERE}"
    )
    assert _column(ch_client, org, rows) == [f"ns2:{_SUMMARY_ID}", f"ns:{_BOB_SUMMARY_ID}"]
    assert _column(ch_client, org, f"SELECT entity_a FROM loom.entity_edges{_WHERE}") == ["bob"]
    links = f"SELECT memory_key FROM loom.entity_edge_memories{_WHERE}"
    assert _column(ch_client, org, links) == ["bob-diet"]
    rows = f"SELECT toString(memory_id) FROM loom.entity_edge_rows{_WHERE}"
    assert _column(ch_client, org, rows) == [_BOB_SUMMARY_ID]
    communities = f"SELECT entity FROM loom.entity_communities FINAL{_WHERE}"
    assert _column(ch_client, org, communities) == ["fish"]
    vectors = f"SELECT concat(user_id, ':', memory_key) FROM loom.entity_vectors{_WHERE}"
    assert _column(ch_client, org, vectors) == ["-:topic-bob", "carol:topic-diets"]
    shared = f"SELECT memory_key FROM loom.shared FINAL{_WHERE} AND tombstoned_at IS NULL"
    assert _column(ch_client, org, shared) == ["topic-bob"]
    shared = f"SELECT memory_key FROM loom.memory_embeddings FINAL{_WHERE} AND scope = 'shared'"
    assert _column(ch_client, org, f"{shared} AND tombstoned_at IS NULL") == ["topic-bob"]
    assert _column(ch_client, org, shared) == (
        ["topic-bob"] if purge else ["topic-bob", "topic-diets"]
    )


async def test_a_reopened_link_delete_takes_the_triples_its_links_alone_held(ch_client, ch) -> None:
    """`graph_links.delete` ran without the summary's links (a build whose step did not
    take them, or a summary linked since): the census reopens it, and it must not leave
    the triple unlinked."""
    org = unique_org("ersumre")
    _seed(ch_client, org)
    ch_client.command(
        "DELETE FROM loom.entity_triple_memories WHERE org = {org:String} "
        "AND memory_key = 'user_diet'",
        parameters={"org": org},
    )
    with erasure_mutations.saga_scope(f"erasure-{org}"):
        steps = [s for s in _STEPS if s[0] in ("memory.tombstone", "graph_links.delete")]
        await _run(steps, org, ch)
    triples = f"SELECT subject FROM loom.entity_triples{_WHERE}"
    assert _column(ch_client, org, triples) == ["bob"]


async def test_a_purge_takes_what_a_summary_written_late_fed(ch_client, ch) -> None:
    """A summary of alice's memory written after the steps that delete what summaries
    fed: in a purge `memory.retire` deletes the rows that name it, so it takes its
    history and chunks first."""
    org = unique_org("ersumlt")
    _seed(ch_client, org)
    late = "00000000-0000-4000-8000-000000000004"
    with erasure_mutations.purge_scope(True), erasure_mutations.saga_scope(f"erasure-{org}"):
        await _run(_STEPS[:-1], org, ch)
        row = [org, "ns", "a", "-", "-", "topic-late", "fact", "alice is vegetarian", 1]
        row += [["consolidated", "src:user_diet"], late]
        _insert(ch_client, "loom.memory", _MEMORY_COLS, [row])
        text = "alice is vegetarian, late"
        _insert(
            ch_client,
            "loom.memory_chunks",
            ["org", "namespace", "agent", "user_id", "session_id", "chunk_index", "content"]
            + ["content_sha256", "parent_memory_key", "embedding"],
            [
                [org, "ns", "a", "-", "-", 0, text, hashlib.sha256(text.encode()).hexdigest()]
                + ["topic-late", _VECTOR]
            ],
        )
        await _run(_STEPS[-1:], org, ch)
    history = f"SELECT memory_key FROM loom.memory_versions{_WHERE}"
    assert "topic-late" not in _column(ch_client, org, history)
    chunks = f"SELECT parent_memory_key FROM loom.memory_chunks{_WHERE}"
    assert "topic-late" not in _column(ch_client, org, chunks)


async def test_outside_a_purge_a_late_summary_reopens_its_steps(ch_client, ch) -> None:
    """A summary of alice's memory written after `memory.retire`, outside a purge: its
    rows stay readable, so the census counts it, and the steps it reopens retire it."""
    org = unique_org("ersumtl")
    _seed(ch_client, org)
    row = [org, "ns", "a", "-", "-", "topic-late", "fact", "alice is vegetarian", 1]
    row += [["consolidated", "src:user_diet"], "00000000-0000-4000-8000-000000000004"]
    with erasure_mutations.saga_scope(f"erasure-{org}"):
        await _run(_STEPS, org, ch)
        _insert(ch_client, "loom.memory", _MEMORY_COLS, [row])
        _seed_shared(ch_client, org, ("topic-late",))  # and promoted at once
        residue = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        assert (residue["loom.memory"], residue["loom.memory_versions"]) == (1, 1)
        reopened = erasure_census.steps_to_reopen(residue, "user_id")
        assert {"memory_versions.delete", "memory.retire"} <= reopened
        await _run([s for s in _STEPS if s[0] in reopened], org, ch)
        after = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
    assert (after["loom.memory"], after["loom.memory_versions"]) == (0, 0)
    # loom.shared is not counted: memory.retire takes the copy
    shared = f"SELECT memory_key FROM loom.shared FINAL{_WHERE} AND tombstoned_at IS NULL"
    assert "topic-late" not in _column(ch_client, org, shared)
    vectors = f"SELECT memory_key FROM loom.memory_embeddings FINAL{_WHERE} AND scope = 'shared'"
    assert "topic-late" not in _column(ch_client, org, f"{vectors} AND tombstoned_at IS NULL")
