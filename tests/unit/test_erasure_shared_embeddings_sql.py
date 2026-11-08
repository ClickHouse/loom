"""A user erasure tombstones the shared embeddings of the namespaces its shared rows
live in, run as real SQL on clickhouse-local: another namespace's shared row of the
same key, promoted from someone else, keeps its embedding."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.erasure.store import steps_memory as memory_steps
from tests.unit.clickhouse_local import CLICKHOUSE, EMBEDDINGS, VECTOR, KeptCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _shared(namespace: str, user_id: str) -> str:
    return (
        "INSERT INTO loom.shared (org, namespace, memory_key, value, memory_type, confidence,"
        " promoted_from, promotion_event, promoted_by, reversible_until, version) VALUES"
        f" ('acme', '{namespace}', 'k1', 'v', 'fact', 1,"
        f" ['acme/{namespace}/bot/{user_id}/-/k1'], generateUUIDv4(), 'sa:p', now(), 1)"
    )


def _embedding(namespace: str) -> str:
    return (
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id,"
        " memory_key, memory_type, scope, content_excerpt, embedding_model, embedding,"
        f" memory_key_embedding) VALUES ('acme', '{namespace}', '_shared', '-', '-', 'k1',"
        f" 'fact', 'shared', 'v', 'm', {VECTOR}, {VECTOR})"
    )


@pytest.mark.asyncio
async def test_a_shared_row_of_the_same_key_in_another_namespace_keeps_its_embedding(
    tmp_path: Path,
) -> None:
    ch = KeptCH(
        tmp_path,
        ddl("shared"),
        ddl("memory"),
        ddl("memory_versions"),
        *EMBEDDINGS,
        _shared("team-a", "alice"),
        _shared("team-b", "bob"),
        _embedding("team-a"),
        _embedding("team-b"),
    )
    n = await memory_steps.tombstone_shared_embeddings_by_user("acme", "alice", ch=ch)
    rows = await ch.query_all(
        "SELECT namespace, tombstoned_at IS NOT NULL AS gone"
        " FROM loom.memory_embeddings FINAL ORDER BY namespace"
    )
    assert n == 1
    assert [(r["namespace"], r["gone"]) for r in rows] == [("team-a", 1), ("team-b", 0)]


def _promoted(namespace: str, key: str, source: str) -> str:
    return (
        "INSERT INTO loom.shared (org, namespace, memory_key, value, memory_type, confidence,"
        " promoted_from, promotion_event, promoted_by, reversible_until, version) VALUES"
        f" ('acme', '{namespace}', '{key}', 'v', 'fact', 1, ['acme/{source}'],"
        " generateUUIDv4(), 'sa:p', now(), 1)"
    )


@pytest.mark.asyncio
async def test_a_copy_promoted_from_a_summary_of_the_users_memory_goes(tmp_path: Path) -> None:
    """The summary's key holds a slash: the entry's key is the rest of it. A copy of
    the same key under another agent is another memory's, and stays."""
    summary = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key,"
        " memory_type, value, version, tags) VALUES"
        " ('acme', 'ns', 'a', 'alice', '-', 'user_diet', 'fact', 'v', 1, []),"
        " ('acme', 'ns', 'a', '-', '-', 'topic/diets', 'fact', 'v', 1,"
        " ['consolidated', 'src:user_diet'])"
    )
    ch = KeptCH(
        tmp_path,
        ddl("shared"),
        ddl("memory"),
        ddl("memory_versions"),
        summary,
        _promoted("ns", "copy-mine", "ns/a/-/-/topic/diets"),
        _promoted("ns", "copy-other", "ns/b/-/-/topic/diets"),
    )
    assert await memory_steps.tombstone_shared_by_user("acme", "alice", ch=ch) == 1
    rows = await ch.query_all(
        "SELECT memory_key, tombstoned_at IS NOT NULL AS gone FROM loom.shared FINAL"
        " ORDER BY memory_key"
    )
    assert [(r["memory_key"], r["gone"]) for r in rows] == [("copy-mine", 1), ("copy-other", 0)]
