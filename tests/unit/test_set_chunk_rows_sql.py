"""The row a pooled memory.set chunk was cut from, read on clickhouse-local against the
real `loom.memory_embeddings`: the writer's newest live row of the key, and none when the
fact leg's filters leave that row out."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval.pool import _set_chunk_rows
from server.memory.domain.search_types import SearchHit
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run
from tests.unit.test_memory_search_service import _q

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_VEC = "[" + ",".join(["0.1"] * 3072) + "]"


def _row(user: str, session: str, version: int, *, valid_to: str = "NULL") -> str:
    return (
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding, memory_key_embedding, "
        "embedding_model, embedding_dim, version, status, valid_to) VALUES ('clickhouse', 'ns', "
        f"'bot', '{user}', '{session}', 'lesson_a', 'semantic', 'private', 't', {_VEC}, {_VEC}, "
        f"'text-embedding-3-small', 3072, {version}, 'active', {valid_to})"
    )


class _LocalCH:
    def __init__(self, *rows: str) -> None:
        self.setup = ["SET enable_quantized_codec = 1", ddl("memory_embeddings"), *rows]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(self.setup, sql, params)


def _chunk(user: str) -> SearchHit:
    return SearchHit(
        memory_key="chunk:memory.set:lesson_a:0",
        memory_type="episodic",
        content_excerpt="a passage",
        distance=0.2,
        source="chunk",
        parent_memory_key="lesson_a",
        identity=("bot", user, "memory.set:lesson_a"),
    )


@pytest.mark.asyncio
async def test_a_chunk_names_its_writers_newest_row_of_the_key() -> None:
    ch = _LocalCH(_row("u", "s1", 1), _row("u", "s2", 2), _row("u2", "s3", 3))
    rows = await _set_chunk_rows(ch, _q(), [_chunk("u")])
    assert rows == {("bot", "u", "lesson_a"): ("bot", "u", "s2")}


@pytest.mark.asyncio
async def test_a_chunk_of_a_row_the_fact_leg_leaves_out_names_none() -> None:
    # the newest row has lapsed: its chunks hold its value, never the older row's
    ch = _LocalCH(_row("u", "s1", 1), _row("u", "s2", 2, valid_to="'2001-01-01 00:00:00'"))
    rows = await _set_chunk_rows(ch, _q(), [_chunk("u")])
    assert rows == {("bot", "u", "lesson_a"): None}
