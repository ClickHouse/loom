"""A side leg leaves out the rows the pool holds, not every row of their keys: one key
holds a row per writer and session."""

from __future__ import annotations

import pytest

from server.memory.domain.search_sql import exclude_pooled
from server.memory.domain.search_types import SearchHit


def _hit(
    key: str,
    identity: tuple[str, str, str] | None,
    source: str = "fact",
) -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=key,
        distance=0.5,
        identity=identity,
        source=source,
    )


def test_a_pooled_row_leaves_out_only_itself() -> None:
    where: list[str] = []
    params: dict = {}
    # a row stored with an empty session is still that row
    exclude_pooled(where, params, [_hit("k", ("a", "u", "s1")), _hit("k", ("b", "u", ""))])
    assert params == {"pooled_rows": [("a", "u", "s1", "k"), ("b", "u", "", "k")]}
    assert len(where) == 1


def test_a_pooled_record_leaves_its_key_open() -> None:
    where: list[str] = []
    params: dict = {}
    exclude_pooled(where, params, [_hit("k", None, source="record")])
    assert (where, params) == ([], {})


def test_nothing_pooled_adds_no_predicate() -> None:
    where: list[str] = []
    params: dict = {}
    exclude_pooled(where, params, [])
    assert (where, params) == ([], {})


@pytest.mark.asyncio
async def test_the_unmatched_leg_binds_the_pooled_rows_not_their_keys() -> None:
    from server.memory.app.retrieval.side_legs import _fetch_unmatched_candidates
    from tests.unit.test_memory_search_service import StubCH, _q

    ch = StubCH()
    ch.query_all_returns.append([])
    await _fetch_unmatched_candidates(ch, _q(), [_hit("k", ("a", "u", "s1"))], limit=5)
    sql, params = ch.query_all_calls[0]
    assert "(e.agent, e.user_id, e.session_id, e.memory_key) NOT IN" in sql
    assert params["pooled_rows"] == [("a", "u", "s1", "k")]


@pytest.mark.asyncio
async def test_an_unmatched_row_carries_its_version() -> None:
    from server.memory.app.retrieval.side_legs import _fetch_unmatched_candidates
    from tests.unit.test_memory_search_service import StubCH, _q

    ch = StubCH()
    row = {"memory_key": "k", "memory_type": "semantic", "content_excerpt": "t", "distance": 0.4}
    ch.query_all_returns.append(
        [{**row, "version": 9, "agent": "a", "user_id": "u", "session_id": "s"}]
    )
    (hit,) = await _fetch_unmatched_candidates(ch, _q(), [], limit=5)
    assert hit.version == 9
