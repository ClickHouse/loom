"""The row a pooled memory.set chunk was cut from, read on clickhouse-local against the
real `loom.memory_embeddings` and `loom.memory`: the writer's served row whose current
value cuts that chunk."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from server.core.ch_params import FIELD_BYTES, chunks
from server.memory.app.retrieval.pool import SetChunkRow, _set_chunk_rows
from server.memory.app.set_chunks import plan_set_chunks, set_chunk_identities
from server.memory.domain.search_types import SearchHit
from tests.unit.clickhouse_local import CLICKHOUSE, EMBEDDINGS, VECTOR, LocalCH, ddl
from tests.unit.test_memory_search_service import _q

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

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
    tags: str = "[]",
    memory_type: str = "semantic",
) -> list[str]:
    """A row's vector at `version` and its stored value."""
    return [
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding, memory_key_embedding, "
        "embedding_model, embedding_dim, version, status, valid_to, tags) VALUES ('clickhouse', "
        f"'ns', 'bot', '{user}', '{session}', 'lesson_a', '{memory_type}', 'private', 't', {VECTOR}, "
        f"{VECTOR}, '{model}', 3072, {version}, '{status}', {valid_to}, {tags})",
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, version, status) VALUES ('clickhouse', 'ns', 'bot', "
        f"'{user}', '{session}', 'lesson_a', '{memory_type}', '{value}', {version}, '{status}')",
    ]


def _local_ch(*rows: list[str]) -> LocalCH:
    return LocalCH(*EMBEDDINGS, ddl("memory"), *(statement for row in rows for statement in row))


def _chunk(user: str, value: Any, index: int = 0) -> SearchHit:
    """The pooled hit of `value`'s chunk `index`, as the chunk leg builds it."""
    passage = plan_set_chunks(value, "semantic", "lesson_a")[0][index][0]
    (sha,) = {s for i, s in set_chunk_identities(value, "semantic", "lesson_a") if i == index}
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
    return {key: SetChunkRow(("bot", "u", session), version, ())}


@pytest.mark.asyncio
async def test_a_chunk_names_the_row_whose_value_cuts_it() -> None:
    ch = _local_ch(_row("u", "s1", 1, LONG_V1), _row("u", "s2", 2, LONG_V2))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s1", 1)


@pytest.mark.asyncio
async def test_a_chunk_no_rows_value_cuts_any_more_names_none() -> None:
    # s1 was re-set short after its long value was chunked: the chunk is stale
    ch = _local_ch(_row("u", "s1", 3, "a short value now"), _row("u", "s0", 1, LONG_V2))
    assert await _set_chunk_rows(ch, _q(), [_chunk("u", LONG_V1)]) == {}


@pytest.mark.asyncio
async def test_two_rows_holding_one_value_give_the_chunk_to_the_newest() -> None:
    ch = _local_ch(_row("u", "s1", 1, LONG_V1), _row("u", "s2", 5, LONG_V1))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s2", 5)


@pytest.mark.asyncio
async def test_two_rows_at_one_version_give_the_chunk_to_one_of_them_always() -> None:
    ch = _local_ch(_row("u", "s2", 4, LONG_V1), _row("u", "s1", 4, LONG_V1))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s1", 4)


@pytest.mark.asyncio
async def test_a_newer_duplicate_of_the_value_leaves_the_chunk_to_the_served_row() -> None:
    ch = _local_ch(_row("u", "s1", 1, LONG_V1), _row("u", "s2", 5, LONG_V1, status="duplicate"))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s1", 1)


@pytest.mark.asyncio
async def test_a_row_the_fact_leg_does_not_serve_names_none() -> None:
    ch = _local_ch(_row("u", "s1", 1, LONG_V1, valid_to="'2001-01-01 00:00:00'"))
    assert await _set_chunk_rows(ch, _q(), [_chunk("u", LONG_V1)]) == {}


@pytest.mark.asyncio
async def test_a_row_embedded_under_another_model_still_names_its_chunks() -> None:
    # the vector checks rank a row; they do not decide whether it is served
    ch = _local_ch(_row("u", "s1", 1, LONG_V1, model="an-older-model"))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s1", 1)


@pytest.mark.asyncio
async def test_a_row_outside_the_hard_tags_is_not_served() -> None:
    ch = _local_ch(_row("u", "s1", 1, LONG_V1))
    assert await _set_chunk_rows(ch, _q(tags=["kind:lesson"]), [_chunk("u", LONG_V1)]) == {}


@pytest.mark.asyncio
async def test_a_row_inside_the_hard_tags_names_its_chunk_with_its_tags() -> None:
    # the hit made from the chunk is stamped with them, so the tag belt judges its own row
    ch = _local_ch(_row("u", "s1", 1, LONG_V1, tags="['kind:lesson', 'x']"))
    chunk = _chunk("u", LONG_V1)
    (named,) = (await _set_chunk_rows(ch, _q(tags=["kind:lesson"]), [chunk])).values()
    assert named == SetChunkRow(("bot", "u", "s1"), 1, ("kind:lesson", "x"))


@pytest.mark.asyncio
async def test_only_the_writers_newest_rows_of_the_key_are_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_set_chunk_rows", 1)
    ch = _local_ch(
        *[_row("u", f"s{i}", i, LONG_V2) for i in range(1, 6)], _row("u", "s9", 9, LONG_V1)
    )
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s9", 9)
    # a value only rows past the bound hold names no row
    assert await _set_chunk_rows(ch, _q(), [_chunk("u", LONG_V2)]) == {}
    monkeypatch.setattr(settings, "memory_search_set_chunk_rows", 64)
    older = _chunk("u", LONG_V2)
    assert await _set_chunk_rows(ch, _q(), [older]) == _named(older, "s5", 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(("category", "named"), [("runbooks", True), ("other", False)])
async def test_a_hard_category_decides_whether_the_row_is_served(
    category: str, named: bool
) -> None:
    # the hard category filter is a subquery inside the HAVING argMax
    ch = _local_ch(
        _row("u", "s1", 1, LONG_V1),
        [
            ddl("categories"),
            ddl("memory_categories"),
            "INSERT INTO loom.categories (org, namespace, category_id, name, embedding) VALUES "
            f"('clickhouse', 'ns', 'c1', 'runbooks', {VECTOR}), ('clickhouse', 'ns', 'c2', 'other', {VECTOR})",
            "INSERT INTO loom.memory_categories (org, namespace, memory_key, category_id) "
            "VALUES ('clickhouse', 'ns', 'lesson_a', 'c1')",
        ],
    )
    chunk = _chunk("u", LONG_V1)
    rows = await _set_chunk_rows(ch, _q(category=category), [chunk])
    assert rows == (_named(chunk, "s1", 1) if named else {})


@pytest.mark.asyncio
async def test_the_writer_keys_fit_one_form_field() -> None:
    # a chunk of a writer and key past the field names no row and drops
    asked: list[dict[str, Any]] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[Any]:
            asked.append(params)
            return []

    base = _chunk("u", LONG_V1)
    keys = [f"{'k' * 200}{i:04d}" for i in range(1000)]
    pooled = [
        replace(base, parent_memory_key=k, memory_key=f"chunk:memory.set:{k}:0") for k in keys
    ]
    assert await _set_chunk_rows(_CH(), _q(), pooled) == {}
    bound = asked[0]["set_rows"]
    every = sorted(("bot", "u", k) for k in keys)
    assert 0 < len(bound) < len(every) and bound == every[: len(bound)]
    assert next(chunks(bound, limit=FIELD_BYTES)) == bound


@pytest.mark.asyncio
async def test_rows_holding_one_value_chunk_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # a key written in three sessions with one value: each row read, the value chunked once
    from server.memory.app.retrieval import pool

    calls: list[str] = []
    real = pool.set_chunk_identities

    def counting(value: Any, memory_type: str, memory_key: str) -> Any:
        calls.append(memory_type)
        return real(value, memory_type, memory_key)

    monkeypatch.setattr(pool, "set_chunk_identities", counting)
    ch = _local_ch(*(_row("u", s, v, LONG_V1) for s, v in (("s1", 1), ("s2", 2), ("s3", 3))))
    chunk = _chunk("u", LONG_V1)
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s3", 3)
    assert calls == ["semantic"]


@pytest.mark.asyncio
async def test_one_text_under_two_types_is_cut_by_each_type() -> None:
    # a procedure chunks from its steps, any other type from its JSON: the newer procedural
    # row does not cut the chunk the older semantic row's text does
    procedure = {
        "name": "release",
        "situation": "a release goes out",
        "steps": [f"step {i}: check the replica lag and the merge queue" for i in range(120)],
    }
    text = json.dumps(procedure)
    ch = _local_ch(_row("u", "s1", 1, text), _row("u", "s2", 2, text, memory_type="procedural"))
    chunk = _chunk("u", procedure)  # a chunk of its semantic (JSON) text
    assert await _set_chunk_rows(ch, _q(), [chunk]) == _named(chunk, "s1", 1)
