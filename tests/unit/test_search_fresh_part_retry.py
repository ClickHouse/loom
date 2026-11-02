"""The chunk / lexical / record legs retry the fresh-part CH code 10 the way
the vector leg does: one leg the caller never asked
about must not turn a search into a 500."""

from __future__ import annotations

import pytest

from server.memory.app.retrieval import retry as M
from server.settings import settings


class _FreshPart(Exception):
    def __init__(self) -> None:
        super().__init__(
            "Code: 10. DB::Exception: Not found column org in block. There are only columns: "
            "(size = 0)"
        )


@pytest.mark.asyncio
async def test_side_leg_retries_the_empty_block_shape_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "memory_retrieval_empty_block_retry_sleep_s", 0.0)
    calls = {"n": 0}

    async def leg() -> list:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FreshPart()
        return ["hit"]

    assert await M._call_retrying_empty_block(leg) == ["hit"]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_side_leg_gives_up_after_the_retry_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_retrieval_empty_block_retry_sleep_s", 0.0)

    async def leg() -> list:
        raise _FreshPart()

    with pytest.raises(_FreshPart):
        await M._call_retrying_empty_block(leg)


@pytest.mark.asyncio
async def test_other_errors_are_not_retried() -> None:
    calls = {"n": 0}

    async def leg() -> list:
        calls["n"] += 1
        raise RuntimeError("something else")

    with pytest.raises(RuntimeError):
        await M._call_retrying_empty_block(leg)
    assert calls["n"] == 1


def _chunk_row() -> dict:
    return {
        "memory_key": "chunk:s1:0",
        "memory_type": "episodic",
        "content_excerpt": "the deploy window is Tuesday",
        "content_full": "the deploy window is Tuesday",
        "session_id": "s1",
        "chunk_index": 0,
        "parent_memory_key": "",
        "distance": 0.1,
        "entity_overlap_count": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_call", [0, 1], ids=["stage1", "stage2"])
async def test_the_chunk_leg_retries_a_fresh_part_error_instead_of_dropping_the_leg(
    monkeypatch: pytest.MonkeyPatch, failing_call: int
) -> None:
    """The chunk leg catches its own errors to fail open, so the fresh-part
    retry has to run inside it: a code 10 on either statement is re-run and
    the leg still answers instead of coming back empty."""
    from server.memory.app.retrieval.chunks import _search_memory_chunks
    from tests.unit.test_memory_search_service import StubCH, _q

    monkeypatch.setattr(settings, "memory_retrieval_empty_block_retry_sleep_s", 0.0)

    class _FlakyCH(StubCH):
        n = 0

        async def query_all(self, sql, params=None):
            call, self.n = self.n, self.n + 1
            if call == failing_call:
                raise _FreshPart()
            return await super().query_all(sql, params)

    ch = _FlakyCH()
    ch.query_all_returns.append([{"session_id": "s1", "chunk_index": 0, "distance": 0.1}])
    ch.query_all_returns.append([_chunk_row()])
    q = _q(entity_filter=[], query_text="", top_k=5)
    hits = await _search_memory_chunks(ch, q, {"q": q.query_vec})

    assert [h.memory_key for h in hits] == ["chunk:s1:0"]
    assert ch.n == 3  # the failed statement ran twice


@pytest.mark.asyncio
async def test_the_chunk_leg_still_fails_open_on_any_other_error() -> None:
    from server.memory.app.retrieval.chunks import _search_memory_chunks
    from tests.unit.test_memory_search_service import _q

    class _BrokenCH:
        calls = 0

        async def query_all(self, sql, params=None):
            self.calls += 1
            raise RuntimeError("Code: 241. DB::Exception: Memory limit exceeded")

    ch = _BrokenCH()
    q = _q(entity_filter=[], query_text="", top_k=5)
    assert await _search_memory_chunks(ch, q, {"q": q.query_vec}) == []
    assert ch.calls == 1


_LEXICAL_ROW = {
    "memory_key": "deploy_window",
    "memory_type": "preference",
    "content_excerpt": "Deploy Tue/Thu, never Friday",
    "distance": 0.2,
    "lex_score": 1,
}
_RECORD_ROW = {
    "record_id": "r1",
    "parent_memory_key": "deploy_window",
    "schema_name": "window",
    "rank_score": 0.8,
    "record_json": "{}",
    "field_text": "deploy window tuesday",
}


def _lexical_leg(ch):
    from server.memory.app.retrieval.side_legs import _lexical_recall_fallback
    from tests.unit.test_memory_search_service import _q

    return _lexical_recall_fallback(ch, _q(query_text="deploy window"))


def _records_leg(ch):
    from server.memory.app.retrieval.side_legs import _search_memory_records
    from tests.unit.test_memory_search_service import _q

    return _search_memory_records(ch, _q(query_text="deploy window"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leg,row",
    [(_lexical_leg, _LEXICAL_ROW), (_records_leg, _RECORD_ROW)],
    ids=["lexical", "records"],
)
async def test_the_side_legs_retry_a_fresh_part_error_instead_of_dropping_the_leg(
    monkeypatch: pytest.MonkeyPatch, leg, row
) -> None:
    """Like the chunk leg, the lexical and records legs fail open inside
    themselves, so the retry runs around their statement: a code 10 is
    re-read and the leg still answers."""
    from tests.unit.test_memory_search_service import StubCH

    monkeypatch.setattr(settings, "memory_retrieval_empty_block_retry_sleep_s", 0.0)

    class _FlakyCH(StubCH):
        n = 0

        async def query_all(self, sql, params=None):
            self.n += 1
            if self.n == 1:
                raise _FreshPart()
            return await super().query_all(sql, params)

    ch = _FlakyCH()
    ch.query_all_returns.append([row])
    hits = await leg(ch)

    assert [h.memory_key for h in hits] == ["deploy_window"]
    assert ch.n == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", [_lexical_leg, _records_leg], ids=["lexical", "records"])
@pytest.mark.parametrize(
    "make_error,calls",
    [
        (lambda: RuntimeError("Code: 241. DB::Exception: Memory limit exceeded"), 1),
        (_FreshPart, 1 + settings.memory_retrieval_empty_block_retries),
    ],
    ids=["other_error", "exhausted_retries"],
)
async def test_the_side_legs_still_fail_open(
    monkeypatch: pytest.MonkeyPatch, leg, make_error, calls
) -> None:
    monkeypatch.setattr(settings, "memory_retrieval_empty_block_retry_sleep_s", 0.0)

    class _BrokenCH:
        n = 0

        async def query_all(self, sql, params=None):
            self.n += 1
            raise make_error()

    ch = _BrokenCH()
    assert await leg(ch) == []
    assert ch.n == calls
