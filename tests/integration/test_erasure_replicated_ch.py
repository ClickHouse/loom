"""The user-scope erasure steps and memory.clear on replicated tables: ClickHouse refuses a
mutation with a subquery or a non-deterministic function there (code 36), so every
statement these steps send must bind its values."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from server.settings import settings
from tests.integration._resources import ch_client_from_env, unique_org

NS = "repl"
USER = "u1"
# The tables the steps below read and mutate: a step that reads one outside this list
# fails here with an unknown table.
_TABLES = (
    "memory",
    "memory_embeddings",
    "memory_chunks",
    "memory_belief_sources",
    "memory_chunk_sources",
    "entity_triple_memories",
    "entity_triples",
    "entity_edge_memories",
    "entity_edges",
    "entity_triple_rows",
    "entity_edge_rows",
    "entity_communities",
    "documents",
    "memory_code_links",
    "memory_versions",
    "shared",
)
# loom.entity_vectors is left out: this fixture's copy of its quantized codec column is
# refused (code 36). Its summary statement binds a tuple array and holds no subquery.
_VECTOR = "arrayWithConstant(3072, 0.)"
_SUMMARY_ID = "00000000-0000-4000-8000-0000000000c1"


@pytest.fixture
def raw() -> Iterator[Any]:
    client = ch_client_from_env(settings={"enable_quantized_codec": 1})
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def db(raw: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A Replicated database holding a ReplicatedMergeTree copy of `_TABLES`, which the
    run takes as its schema."""
    name = f"erasure_repl_{uuid.uuid4().hex[:8]}"
    source = settings.ch_database
    raw.command(
        f"CREATE DATABASE {name} ENGINE = Replicated('/clickhouse/databases/{name}', 's1', 'r1')"
    )
    try:
        for table in _TABLES:
            engine = raw.query(
                "SELECT engine_full FROM system.tables WHERE database = %(db)s AND name = %(t)s",
                parameters={"db": source, "t": table},
            ).result_rows[0][0]
            raw.command(
                f"CREATE TABLE {name}.{table} AS {source}.{table} ENGINE = Replicated{engine}"
            )
        engines = raw.query(
            "SELECT DISTINCT engine LIKE 'Replicated%%' FROM system.tables WHERE database = %(db)s",
            parameters={"db": name},
        ).result_rows
        assert engines == [(True,)]
        monkeypatch.setattr(settings, "ch_database", name)
        yield name
    finally:
        raw.command(f"DROP DATABASE IF EXISTS {name} SYNC")


@pytest.fixture
async def ch(db: str) -> AsyncIterator[Any]:
    from server.core.ch import CHClient

    client = CHClient()
    await client.connect()
    yield client
    await client.disconnect()


def _seed(raw: Any, db: str, org: str) -> None:
    """One user's memory under every table, a locked row the clear keeps, another
    writer's version of the user's document, and a summary of the user's memory with its
    history, chunk, row link and a shared copy with its search row."""
    ident = "'a', 'u1', 's1'"
    for key, lock in (("k1", ""), ("k2", "read_only")):
        raw.command(
            f"INSERT INTO {db}.memory (org, namespace, agent, user_id, session_id, memory_key,"
            " version, memory_id, lock_mode)"
            f" SELECT '{org}', '{NS}', {ident}, '{key}', 1, generateUUIDv4(), '{lock}'"
        )
        raw.command(
            f"INSERT INTO {db}.memory_embeddings (org, namespace, agent, user_id, session_id,"
            " memory_key, version, embedding, memory_key_embedding)"
            f" SELECT '{org}', '{NS}', {ident}, '{key}', 1, {_VECTOR}, {_VECTOR}"
        )
    raw.command(
        f"INSERT INTO {db}.memory_chunks (org, namespace, agent, user_id, session_id,"
        " parent_memory_key, chunk_index, content, embedding)"
        f" SELECT '{org}', '{NS}', {ident}, 'k1', 0, 'chunk', {_VECTOR}"
    )
    for table in ("memory_belief_sources", "memory_chunk_sources"):
        raw.command(
            f"INSERT INTO {db}.{table} (org, namespace, memory_key) VALUES ('{org}', '{NS}', 'k1')"
        )
    raw.command(
        f"INSERT INTO {db}.entity_triple_memories (org, namespace, subject, relation, object,"
        f" memory_key) VALUES ('{org}', '{NS}', 'user', 'likes', 'oslo', 'k1')"
    )
    raw.command(
        f"INSERT INTO {db}.entity_triples (org, namespace, subject, relation, object)"
        f" VALUES ('{org}', '{NS}', 'user', 'likes', 'oslo')"
    )
    raw.command(
        f"INSERT INTO {db}.entity_edge_memories (org, namespace, entity_a, entity_b, memory_key)"
        f" VALUES ('{org}', '{NS}', 'oslo', 'user', 'k1')"
    )
    raw.command(
        f"INSERT INTO {db}.entity_edges (org, namespace, entity_a, entity_b)"
        f" VALUES ('{org}', '{NS}', 'oslo', 'user')"
    )
    raw.command(
        f"INSERT INTO {db}.entity_communities (org, namespace, entity, community_id, label)"
        f" VALUES ('{org}', '{NS}', 'oslo', 1, 'oslo')"
    )
    # the user's own history of k1 and a row link of the user's
    raw.command(
        f"INSERT INTO {db}.memory_versions (org, namespace, agent, user_id, session_id,"
        f" memory_key, version) VALUES ('{org}', '{NS}', {ident}, 'k1', 1)"
    )
    raw.command(
        f"INSERT INTO {db}.entity_triple_rows (org, namespace, memory_id, user_id, subject,"
        f" relation, object) VALUES ('{org}', '{NS}', generateUUIDv4(), 'u1', 'u', 'r', 'o')"
    )
    # a summary of the user's k1 under '-', its history, a chunk and a row link
    tags = "['consolidated', 'src:k1']"
    for table in ("memory", "memory_versions"):
        raw.command(
            f"INSERT INTO {db}.{table} (org, namespace, agent, user_id, session_id, memory_key,"
            f" version, memory_id, tags) VALUES ('{org}', '{NS}', 'a', '-', '-', 'topic', 1,"
            f" '{_SUMMARY_ID}', {tags})"
        )
    raw.command(
        f"INSERT INTO {db}.memory_chunks (org, namespace, agent, user_id, session_id,"
        " parent_memory_key, chunk_index, content, embedding)"
        f" SELECT '{org}', '{NS}', 'a', '-', '-', 'topic', 0, 'summary chunk', {_VECTOR}"
    )
    raw.command(
        f"INSERT INTO {db}.entity_triple_rows (org, namespace, memory_id, user_id, subject,"
        f" relation, object) VALUES ('{org}', '{NS}', '{_SUMMARY_ID}', '-', 's', 'r', 'o')"
    )
    raw.command(
        f"INSERT INTO {db}.shared (org, namespace, memory_key, value, memory_type, confidence,"
        " promoted_from, promotion_event, promoted_by, reversible_until, version)"
        f" VALUES ('{org}', '{NS}', 'team-topic', 'v', 'fact', 1, ['{org}/{NS}/a/-/-/topic'],"
        " generateUUIDv4(), 'explicit', now(), 1)"
    )
    raw.command(
        f"INSERT INTO {db}.memory_embeddings (org, namespace, agent, user_id, session_id,"
        " memory_key, scope, version, embedding, memory_key_embedding)"
        f" SELECT '{org}', '{NS}', '_shared', '-', '-', 'team-topic', 'shared', 1, {_VECTOR},"
        f" {_VECTOR}"
    )
    for who, version in (("u2", 1), (USER, 2)):
        raw.command(
            f"INSERT INTO {db}.documents (org, namespace, source_uri, content, version, user_id)"
            f" VALUES ('{org}', '{NS}', 'doc://a', 'text', {version}, '{who}')"
        )


async def test_the_user_steps_and_a_clear_mutate_replicated_tables(
    raw: Any, db: str, ch: Any
) -> None:
    from server.erasure.store import steps_graph, steps_memory
    from server.erasure.store.mutations import purge_scope
    from server.memory.store.lifecycle import bulk_tombstone_scope

    org = unique_org("erasure-repl")
    _seed(raw, db, org)

    # the user's chunk and the summary's
    assert await steps_memory.tombstone_memory_chunks_by_user(org, USER, ch=ch) == 2
    assert await steps_graph.tombstone_source_links_by_user(org, USER, ch=ch) == 2
    # the triple, the edge and the community, then the user's and the summary's row links
    # and two key links
    assert await steps_graph.delete_graph_links_by_user(org, USER, ch=ch) == 7
    # the summary's shared copy and its search row, then the user's and the summary's
    # history
    assert await steps_memory.tombstone_shared_embeddings_by_user(org, USER, ch=ch) == 1
    assert await steps_memory.tombstone_shared_by_user(org, USER, ch=ch) == 1
    assert await steps_memory.delete_memory_versions_by_user(org, USER, ch=ch) == 2
    with purge_scope(True):
        assert await steps_memory.tombstone_documents_by_user(org, USER, ch=ch) == 1
    counts = await bulk_tombstone_scope(org=org, namespace=NS, user_id=USER, ch=ch)
    assert counts == {"loom.memory": 1, "loom.memory_embeddings": 1, "locked_skipped": 1}

    left = {
        table: raw.query(
            f"SELECT count() FROM {db}.{table} WHERE org = %(o)s", {"o": org}
        ).result_rows[0][0]
        for table in ("entity_triples", "entity_edges", "entity_communities", "memory_versions")
    }
    assert left == dict.fromkeys(
        ("entity_triples", "entity_edges", "entity_communities", "memory_versions"), 0
    )
    for table in ("memory_chunks", "shared"):
        kept = raw.query(
            f"SELECT count() FROM {db}.{table} FINAL WHERE org = %(o)s AND tombstoned_at IS NULL",
            {"o": org},
        ).result_rows[0][0]
        assert kept == 0, table
    live = raw.query(
        f"SELECT memory_key FROM {db}.memory_embeddings FINAL"
        " WHERE org = %(o)s AND tombstoned_at IS NULL",
        {"o": org},
    ).result_rows
    assert live == [("k2",)]
    superseded = raw.query(
        f"SELECT user_id, superseded_at IS NOT NULL FROM {db}.documents WHERE org = %(o)s",
        {"o": org},
    ).result_rows
    assert superseded == [("u2", True)]


async def test_the_consolidated_step_binds_its_pairs_on_replicated_tables(
    raw: Any, db: str, ch: Any
) -> None:
    """The step binds the user's (namespace, tag) pairs and the summaries' (namespace, id),
    so replicated tables take its mutations; a summary another namespace built from a key
    of the same name keeps its rows and its code link."""
    from server.erasure.store import steps_graph

    org = unique_org("erasure-repl")
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    raw.command(
        f"INSERT INTO {db}.memory (org, namespace, agent, user_id, session_id, memory_key,"
        f" version, memory_id) SELECT '{org}', '{NS}', 'a', '{USER}', 's1', 'prefs', 1,"
        " generateUUIDv4()"
    )
    for ns, mid in ((NS, mine), ("elsewhere", theirs)):
        tags = "['consolidated', 'src:prefs']"
        raw.command(
            f"INSERT INTO {db}.memory (org, namespace, agent, user_id, session_id, memory_key,"
            f" version, memory_id, tags) SELECT '{org}', '{ns}', 'a', '-', '-', 'profile', 1,"
            f" toUUID('{mid}'), {tags}"
        )
        raw.command(
            f"INSERT INTO {db}.memory_embeddings (org, namespace, agent, user_id, session_id,"
            " memory_key, version, embedding, memory_key_embedding, tags)"
            f" SELECT '{org}', '{ns}', 'a', '-', '-', 'profile', 1, {_VECTOR}, {_VECTOR}, {tags}"
        )
        raw.command(
            f"INSERT INTO {db}.memory_code_links (org, code_namespace, path, qualified_name,"
            f" namespace, memory_id, memory_key, state) VALUES ('{org}', 'code', 'p', 'q',"
            f" '{ns}', '{mid}', 'profile', 'fresh')"
        )
    assert await steps_graph.tombstone_consolidated_dependents_by_user(org, USER, ch=ch) == 3
    live = {
        table: raw.query(
            f"SELECT namespace FROM {db}.{table} FINAL WHERE org = %(o)s"
            " AND memory_key = 'profile' AND tombstoned_at IS NULL",
            {"o": org},
        ).result_rows
        for table in ("memory", "memory_embeddings")
    }
    assert live == {"memory": [("elsewhere",)], "memory_embeddings": [("elsewhere",)]}
    links = raw.query(
        f"SELECT namespace FROM {db}.memory_code_links WHERE org = %(o)s", {"o": org}
    ).result_rows
    assert links == [("elsewhere",)]


async def test_a_user_with_chunks_and_no_memory_keys_binds_an_empty_slice(
    raw: Any, db: str, ch: Any
) -> None:
    """A transcript chunk under the user, with no memory row naming its key: the step
    still runs once, binding no keys, and reaches the chunk by its `user_id`."""
    from server.erasure.store import steps_memory

    org = unique_org("erasure-repl")
    raw.command(
        f"INSERT INTO {db}.memory_chunks (org, namespace, agent, user_id, session_id,"
        " parent_memory_key, chunk_index, content, embedding)"
        f" SELECT '{org}', '{NS}', 'a', '{USER}', 's1', 'k1', 0, 'chunk', {_VECTOR}"
    )
    assert await steps_memory.tombstone_memory_chunks_by_user(org, USER, ch=ch) == 1


async def test_a_clear_past_one_mutations_worth_of_locks_keeps_every_locked_row_live(
    raw: Any, db: str, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.store import lifecycle

    monkeypatch.setattr(lifecycle, "_LOCKED_PER_MUTATION", 100)
    org = unique_org("erasure-repl")
    for table, extra_cols, extra_vals in (
        (
            "memory",
            ", memory_id, lock_mode",
            ", generateUUIDv4(), if(number < 250, 'read_only', '')",
        ),
        ("memory_embeddings", ", embedding, memory_key_embedding", f", {_VECTOR}, {_VECTOR}"),
    ):
        raw.command(
            f"INSERT INTO {db}.{table} (org, namespace, agent, user_id, session_id, memory_key,"
            f" version{extra_cols})"
            f" SELECT '{org}', '{NS}', 'a', '{USER}', 's1', concat('k', toString(number)), 1"
            f"{extra_vals} FROM numbers(300)"
        )
    counts = await lifecycle.bulk_tombstone_scope(org=org, namespace=NS, user_id=USER, ch=ch)
    assert counts == {"loom.memory": 50, "loom.memory_embeddings": 50, "locked_skipped": 250}
    live = raw.query(
        f"SELECT count() FROM {db}.memory_embeddings FINAL"
        " WHERE org = %(o)s AND tombstoned_at IS NULL",
        {"o": org},
    ).result_rows
    assert live == [(250,)]


def test_a_mutation_with_a_subquery_is_refused(raw: Any, db: str) -> None:
    """What the steps avoid: the form they replaced, refused on the same tables."""
    from clickhouse_connect.driver.exceptions import DatabaseError

    with pytest.raises(DatabaseError, match="Code: 36"):
        raw.command(
            f"ALTER TABLE {db}.entity_triples DELETE WHERE (namespace, subject) IN"
            f" (SELECT namespace, memory_key FROM {db}.memory)"
        )
    with pytest.raises(DatabaseError, match="Code: 36"):
        raw.command(f"ALTER TABLE {db}.memory UPDATE tombstoned_at = now() WHERE 1")
