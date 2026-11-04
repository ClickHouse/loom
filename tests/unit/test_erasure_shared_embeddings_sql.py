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
