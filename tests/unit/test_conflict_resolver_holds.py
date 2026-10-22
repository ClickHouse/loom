"""A held conflict leaves its scan place to another writer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.memory.store import conflict_resolver as queue
from tests.unit import clickhouse_local


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize("recheck", [False, True])
def test_a_held_conflict_does_not_fill_the_queue_limit(recheck: bool) -> None:
    mark = "NULL" if recheck else "toDateTime(1, 'UTC')"
    status = "conflicted" if recheck else "active"
    held_age = 10 if recheck else 1
    held = ("org", "ns", "agent", "user", "session", "key")
    setup = [
        "SET enable_quantized_codec = 1",
        *(clickhouse_local.ddl(t) for t in ("memory_embeddings", "namespaces", "erasure_requests")),
    ]
    setup += [
        "INSERT INTO loom.memory_embeddings "
        "(org, namespace, agent, user_id, session_id, memory_key, scope, embedding_dim, "
        "embedding, memory_key_embedding, embedding_model, status, conflict_checked_at, created_at) VALUES "
        f"('org', 'ns', 'agent', 'user', 'session', 'key', 'private', 3072, arrayWithConstant(3072, toFloat32(1)), arrayWithConstant(3072, toFloat32(1)), 'm', "
        f"'{status}', {mark}, now() - INTERVAL {held_age} MINUTE), "
        f"('org', 'ns', 'another-agent', 'user', 'session', 'key', 'private', 3072, arrayWithConstant(3072, toFloat32(1)), arrayWithConstant(3072, toFloat32(1)), 'm', "
        f"'{status}', {mark}, now() - INTERVAL 5 MINUTE)"
    ]

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return clickhouse_local.run(setup, sql, params)

    read = queue.unjudged_conflicted_rows if recheck else queue.owed_rows
    before = asyncio.run(read(limit=1, ch=_CH()))
    assert [r["agent"] for r in before] == ["agent"]
    after = asyncio.run(read(limit=1, held=[held], ch=_CH()))
    assert [r["agent"] for r in after] == ["another-agent"]
