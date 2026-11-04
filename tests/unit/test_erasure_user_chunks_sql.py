"""A user erasure tombstones the user's verbatim chunks, run as real SQL on
clickhouse-local: another user's chunks of the same (namespace, memory_key) stay
live. Users share a key within a namespace (`user_employer`), and each memory's
chunks carry their writer's user_id; a no-user ('-') chunk of the key may be the
user's legacy chunk and goes with them, as the write path retires it."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.erasure.store import steps_memory as memory_steps
from server.memory.app.set_chunks import set_chunk_session_id
from tests.unit.clickhouse_local import CLICKHOUSE, VECTOR, KeptCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _memory(user_id: str) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, memory_key, version)"
        f" VALUES ('o', 'app', 'bot', '{user_id}', 'user_employer', 1)"
    )


def _chunk(user_id: str, index: int) -> str:
    return (
        "INSERT INTO loom.memory_chunks (org, namespace, agent, user_id, session_id,"
        " chunk_index, content_sha256, content, embedding, embedding_model, parent_memory_key)"
        f" VALUES ('o', 'app', 'bot', '{user_id}', '{set_chunk_session_id('user_employer')}',"
        f" {index}, repeat('{index}', 64), 'works at {user_id}', {VECTOR}, 'm', 'user_employer')"
    )


@pytest.mark.asyncio
async def test_another_users_chunks_of_the_same_key_stay_live(tmp_path: Path) -> None:
    ch = KeptCH(
        tmp_path,
        "SET enable_quantized_codec = 1",
        ddl("memory"),
        ddl("memory_chunks"),
        _memory("u1"),
        _memory("u2"),
        _chunk("u1", 0),
        _chunk("u2", 1),
        _chunk("-", 2),
    )
    n = await memory_steps.tombstone_memory_chunks_by_user("o", "u1", ch=ch)
    rows = await ch.query_all(
        "SELECT user_id, tombstoned_at IS NOT NULL AS gone"
        " FROM loom.memory_chunks FINAL ORDER BY chunk_index"
    )
    assert [(r["user_id"], r["gone"]) for r in rows] == [("u1", 1), ("u2", 0), ("-", 1)]
    assert n == 2
