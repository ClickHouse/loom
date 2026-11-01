"""The row a pooled memory.set chunk was cut from, read on clickhouse-local against the
real `loom.memory_embeddings`: the writer's newest row of the key, when the fact leg
serves it."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval.pool import SetChunkRow, _set_chunk_rows
from server.memory.domain.search_types import SearchHit
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run
from tests.unit.test_memory_search_service import _q

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_VEC = "[" + ",".join(["0.1"] * 3072) + "]"


def _row(
    user: str,
    session: str,
    version: int,
    *,
    history: list[tuple[int, str]] | None = None,
    status: str = "active",
    valid_to: str = "NULL",
    tombstoned: bool = False,
    model: str = "text-embedding-3-small",
) -> list[str]:
    """A row's vector at `version` and its value history, (version, value) oldest first:
    by default one write of the value `session` at `version`."""
    history = history or [(version, session)]
    current = history[-1][1].ljust(64, "0")
    gone = "'2001-01-01 00:00:00'" if tombstoned else "NULL"
    return [
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding, memory_key_embedding, "
        "embedding_model, embedding_dim, version, status, valid_to, tombstoned_at, "
        "content_sha256) VALUES ('clickhouse', 'ns', 'bot', "
        f"'{user}', '{session}', 'lesson_a', 'semantic', 'private', 't', {_VEC}, {_VEC}, "
        f"'{model}', 3072, {version}, '{status}', {valid_to}, {gone}, '{current}')",
        *(
            "INSERT INTO loom.memory_versions (org, namespace, agent, user_id, session_id, "
            "memory_key, version, content_sha256) VALUES ('clickhouse', 'ns', 'bot', "
            f"'{user}', '{session}', 'lesson_a', {v}, '{value.ljust(64, '0')}')"
            for v, value in history
        ),
    ]


class _LocalCH:
    def __init__(self, *rows: list[str]) -> None:
        self.setup = [
            "SET enable_quantized_codec = 1",
            ddl("memory_embeddings"),
            ddl("memory_versions"),
            *(statement for row in rows for statement in row),
        ]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(self.setup, sql, params)


def _chunk(user: str, cut_at: str = "") -> SearchHit:
    return SearchHit(
        memory_key="chunk:memory.set:lesson_a:0",
        memory_type="episodic",
        content_excerpt="a passage",
        distance=0.2,
        source="chunk",
        parent_memory_key="lesson_a",
        identity=("bot", user, "memory.set:lesson_a"),
        created_at=cut_at,
    )


def _row_of(session: str, version: int) -> dict[Any, Any]:
    return {("bot", "u", "lesson_a"): SetChunkRow(("bot", "u", session), version)}


@pytest.mark.asyncio
async def test_a_chunk_names_its_writers_latest_written_row_of_the_key() -> None:
    ch = _LocalCH(_row("u", "s1", 1), _row("u", "s2", 2), _row("u2", "s3", 3))
    assert await _set_chunk_rows(ch, _q(), [_chunk("u")]) == _row_of("s2", 2)


@pytest.mark.asyncio
async def test_a_later_status_change_of_an_older_row_does_not_take_the_chunks() -> None:
    # s1's vector was re-written at version 3 when s2's write marked both conflicted
    ch = _LocalCH(
        _row("u", "s1", 3, history=[(1, "x"), (3, "x")], status="conflicted"),
        _row("u", "s2", 2, status="conflicted"),
    )
    assert await _set_chunk_rows(ch, _q(), [_chunk("u")]) == _row_of("s2", 2)


@pytest.mark.asyncio
async def test_a_value_that_came_back_counts_from_its_return() -> None:
    # s1 wrote x, then w, then (after s2 wrote y) x again: the chunks hold s1's x
    ch = _LocalCH(
        _row("u", "s1", 30, history=[(10, "x"), (15, "w"), (30, "x")]),
        _row("u", "s2", 20),
    )
    assert await _set_chunk_rows(ch, _q(), [_chunk("u")]) == _row_of("s1", 30)


@pytest.mark.asyncio
async def test_a_value_restored_after_another_sessions_delete_names_its_row() -> None:
    ch = _LocalCH(
        _row("u", "s1", 30, history=[(10, "x"), (30, "x")]),
        _row("u", "s2", 20, tombstoned=True),
    )
    assert await _set_chunk_rows(ch, _q(), [_chunk("u")]) == _row_of("s1", 30)


@pytest.mark.asyncio
async def test_a_newest_row_the_fact_leg_leaves_out_names_none() -> None:
    # the newest row has lapsed: its chunks hold its value, never the older row's
    ch = _LocalCH(_row("u", "s1", 1), _row("u", "s2", 2, valid_to="'2001-01-01 00:00:00'"))
    assert await _set_chunk_rows(ch, _q(), [_chunk("u")]) == {}


@pytest.mark.asyncio
async def test_a_row_embedded_under_another_model_still_names_its_chunks() -> None:
    # the vector checks rank a row; they do not decide whether it is served
    ch = _LocalCH(_row("u", "s1", 1), _row("u", "s2", 2, model="an-older-model"))
    assert await _set_chunk_rows(ch, _q(), [_chunk("u")]) == _row_of("s2", 2)


@pytest.mark.asyncio
async def test_a_row_outside_the_hard_tags_is_not_served() -> None:
    ch = _LocalCH(_row("u", "s1", 1))
    assert await _set_chunk_rows(ch, _q(tags=["kind:lesson"]), [_chunk("u")]) == {}


@pytest.mark.asyncio
async def test_under_shared_scope_no_row_is_read() -> None:
    assert await _set_chunk_rows(_LocalCH(), _q(scope="shared"), [_chunk("u")]) is None


@pytest.mark.asyncio
async def test_a_value_written_after_the_chunks_were_cut_did_not_cut_them() -> None:
    # s1's long value was chunked at 10:00; s2's short value at 10:05 cut no chunks
    at_10_00, at_10_05 = 1_767_261_600 * 10**9, 1_767_261_900 * 10**9
    ch = _LocalCH(_row("u", "s1", at_10_00), _row("u", "s2", at_10_05))
    rows = await _set_chunk_rows(ch, _q(), [_chunk("u", cut_at="2026-01-01 10:00:00")])
    assert rows == _row_of("s1", at_10_00)
