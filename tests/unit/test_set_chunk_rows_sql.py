"""The row a pooled memory.set chunk was cut from, read on clickhouse-local against the
real `loom.memory_embeddings` and `loom.memory`: the writer's served row whose current
value cuts that chunk."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval.pool import SetChunkRow, _set_chunk_rows
from server.memory.app.set_chunks import plan_set_chunks, set_chunk_identities
from server.memory.domain.search_types import SearchHit
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run
from tests.unit.test_memory_search_service import _q

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_VEC = "[" + ",".join(["0.1"] * 3072) + "]"
LONG_V1 = "The replica lagged after the merge storm. " * 80
LONG_V2 = "The tier was resized and the lag cleared. " * 80


def _row(
    user: str,
    session: str,
    version: int,
    value: str,
    *,
    status: str = "active",
    valid_to: str = "NULL",
    model: str = "text-embedding-3-small",
) -> list[str]:
    """A row's vector at `version` and its stored value."""
    return [
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding, memory_key_embedding, "
        "embedding_model, embedding_dim, version, status, valid_to) VALUES ('clickhouse', 'ns', "
        f"'bot', '{user}', '{session}', 'lesson_a', 'semantic', 'private', 't', {_VEC}, {_VEC}, "
        f"'{model}', 3072, {version}, '{status}', {valid_to})",
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, version, status) VALUES ('clickhouse', 'ns', 'bot', "
        f"'{user}', '{session}', 'lesson_a', 'semantic', '{value}', {version}, '{status}')",
    ]


class _LocalCH:
    def __init__(self, *rows: list[str]) -> None:
        self.setup = [
            "SET enable_quantized_codec = 1",
            ddl("memory_embeddings"),
            ddl("memory"),
            *(statement for row in rows for statement in row),
        ]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(self.setup, sql, params)


def _chunk(user: str, value: str, index: int = 0) -> SearchHit:
    """The pooled hit of `value`'s chunk `index`, as the chunk leg builds it."""
    passage = plan_set_chunks(value, "semantic")[0][index][0]
    (sha,) = {s for i, s in set_chunk_identities(value, "semantic") if i == index}
    return SearchHit(
        memory_key=f"chunk:memory.set:lesson_a:{index}",
        memory_type="episodic",
        content_excerpt=passage,
        distance=0.2,
        source="chunk",
        parent_memory_key="lesson_a",
        identity=("bot", user, "memory.set:lesson_a"),
        chunk_sha256=sha,
    )


def _named(chunk: SearchHit, session: str, version: int) -> dict[Any, SetChunkRow]:
    key = ("bot", "u", "lesson_a", 0, chunk.chunk_sha256)
    return {key: SetChunkRow(("bot", "u", session), version)}


@pytest.mark.asyncio
async def test_a_chunk_names_the_row_whose_value_cuts_it() -> None:
    ch = _LocalCH(_row("u", "s1", 1, LONG_V1), _row("u", "s2", 2, LONG_V2))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s1", 1)


@pytest.mark.asyncio
async def test_a_chunk_no_rows_value_cuts_any_more_names_none() -> None:
    # s1 was re-set short after its long value was chunked: the chunk is stale
    ch = _LocalCH(_row("u", "s1", 3, "a short value now"), _row("u", "s0", 1, LONG_V2))
    assert await _set_chunk_rows(ch, _q(), [_chunk("u", LONG_V1)]) == {}


@pytest.mark.asyncio
async def test_two_rows_holding_one_value_give_the_chunk_to_the_newest() -> None:
    ch = _LocalCH(_row("u", "s1", 1, LONG_V1), _row("u", "s2", 5, LONG_V1))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s2", 5)


@pytest.mark.asyncio
async def test_a_row_the_fact_leg_does_not_serve_names_none() -> None:
    ch = _LocalCH(_row("u", "s1", 1, LONG_V1, valid_to="'2001-01-01 00:00:00'"))
    assert await _set_chunk_rows(ch, _q(), [_chunk("u", LONG_V1)]) == {}


@pytest.mark.asyncio
async def test_a_row_embedded_under_another_model_still_names_its_chunks() -> None:
    # the vector checks rank a row; they do not decide whether it is served
    ch = _LocalCH(_row("u", "s1", 1, LONG_V1, model="an-older-model"))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s1", 1)


@pytest.mark.asyncio
async def test_a_row_outside_the_hard_tags_is_not_served() -> None:
    ch = _LocalCH(_row("u", "s1", 1, LONG_V1))
    assert await _set_chunk_rows(ch, _q(tags=["kind:lesson"]), [_chunk("u", LONG_V1)]) == {}


@pytest.mark.asyncio
async def test_under_shared_scope_no_row_is_read() -> None:
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(_LocalCH(), _q(scope="shared"), [chunk]) is None
