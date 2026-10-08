"""A repeated code.search is a point read: the key covers what
shapes a result and nothing else; a hit replays the response with the route
suffixed, the current freshness block, and the memo's commit disclosed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
from server.code.app import search_memo as memo
from server.code.domain.units import CodeUnit


def _inp(**kw):
    base = dict(query="how does the parser resolve aliases", top_k=5)
    base.update(kw)
    return CodeSearchInput(**base)


def _key(inp, **kw):
    base = dict(
        embedding_model="text-embedding-3-large", rerank="", token_budget=3000, recent_dirs=[]
    )
    base.update(kw)
    return memo.key_for(inp, **base)


def test_key_ignores_attribution_and_whitespace_but_not_shape() -> None:
    k = _key(_inp())
    assert k == _key(_inp(agent="bot", consumer="claude-code"))  # attribution only
    assert k == _key(_inp(query="how  does the parser\nresolve aliases"))  # whitespace
    assert k != _key(_inp(top_k=6))
    assert k != _key(_inp(path_prefix="src/Parsers"))
    assert k != _key(_inp(bodies="none"))
    assert k != _key(_inp(), embedding_model="text-embedding-3-small")
    assert k != _key(_inp(), rerank="builtin:openai")
    assert k != _key(_inp(), token_budget=1200)
    assert k != _key(_inp(), recent_dirs=["src/Parsers"])
    assert _key(_inp(), recent_dirs=["b", "a"]) == _key(_inp(), recent_dirs=["a", "b"])


def test_another_build_or_ranking_setting_computes_its_own_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    k = _key(_inp())
    assert k == _key(_inp())
    monkeypatch.setattr(settings, "build_commit", "other-build")
    assert _key(_inp()) != k
    monkeypatch.undo()
    assert _key(_inp()) == k
    monkeypatch.setattr(settings, "code_search_medium_risk_from", 0.9)
    assert _key(_inp()) != k


def test_fresh_bypasses_the_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_search_memo_enabled", True)
    assert memo.enabled(_inp())
    assert not memo.enabled(_inp(fresh=True))
    monkeypatch.setattr(settings, "code_search_memo_enabled", False)
    assert not memo.enabled(_inp())


def _output() -> CodeSearchOutput:
    unit = CodeUnit(
        unit_id="c1",
        path="src/Parsers/ParserAlias.cpp",
        start_line=10,
        end_line=40,
        symbol="DB::ParserAlias::parseImpl",
        kind="function",
        score=0.83,
        body="bool ParserAlias::parseImpl(...) { ... }",
    )
    return CodeSearchOutput(
        results=[unit],
        route="hybrid",
        tokens_returned=210,
        retrieval_risk="low",
        retrieval_confidence=0.8,
        score_explanation="rrf",
        retrieval_work={"lexical_rows": 200},
        as_of={"commit": "a" * 40, "stale": False},
        trace_run_id="run-1",
    )


def test_replay_round_trips_units_and_discloses_the_memo_commit_and_age() -> None:
    out = _output()
    payload = out.model_dump_json(exclude={"trace_run_id", "as_of"})
    row = {
        "payload": payload,
        "indexed_commit": ("a" * 40).encode(),  # FixedString reads are bytes
        "route": "hybrid",
        "created_at": datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=90),
    }
    got = memo.replay(
        row, CodeSearchOutput, as_of={"commit": "b" * 40, "stale": False}, trace_run_id="run-2"
    )
    assert got.route == "hybrid+memo" and got.trace_run_id == "run-2"
    assert [u.unit_id for u in got.results] == ["c1"]
    assert got.results[0].body == out.results[0].body and got.results[0].score == 0.83
    assert got.tokens_returned == 210 and got.retrieval_work == {"lexical_rows": 200, "memo_hit": 1}
    assert got.as_of["commit"] == "b" * 40  # the CURRENT block
    assert got.as_of["memo_commit"] == "a" * 40 and got.as_of["memo_stale"] is True
    assert 85 <= got.as_of["memo_age_s"] <= 100


def test_replay_of_a_current_memo_is_not_stale_and_does_not_double_suffix() -> None:
    out = _output()
    row = {
        "payload": out.model_dump_json(exclude={"trace_run_id", "as_of"}),
        "indexed_commit": "a" * 40,
        "route": "hybrid",
        "created_at": datetime.now(UTC).replace(tzinfo=None),
    }
    got = memo.replay(row, CodeSearchOutput, as_of={"commit": "a" * 40}, trace_run_id="r")
    assert got.as_of["memo_stale"] is False
    again = memo.replay(
        {**row, "payload": got.model_dump_json(exclude={"trace_run_id", "as_of"})},
        CodeSearchOutput,
        as_of={},
        trace_run_id="r",
    )
    assert again.route == "hybrid+memo"


@pytest.mark.asyncio
async def test_put_respects_the_payload_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_search_memo_max_payload_bytes", 10)
    before = set(memo._TASKS)
    memo.put("o", "n", "k", _output(), indexed_commit="a" * 40)
    assert set(memo._TASKS) == before  # over the cap: nothing scheduled, nothing awaited


@pytest.mark.asyncio
async def test_the_memo_row_is_attributed_to_the_request_that_served_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ch as ch_mod

    seen: list[str] = []

    class _CH:
        async def insert(self, table, rows, settings=None):
            seen.append(ch_mod.current_query_tag())

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    ch_mod.set_query_tag("code.search req-1")
    try:
        memo.put("o", "n", "k", _output(), indexed_commit="a" * 40)
        await asyncio.gather(*list(memo._TASKS))
    finally:
        ch_mod.set_query_tag("")
    assert seen == ["code.search req-1"]


@pytest.mark.asyncio
async def test_memo_and_pool_rows_read_and_write_loom_code_search_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memo and the cursor pool share one table through the code store:
    a TTL-bounded point read under (org, namespace, key) and a fire-and-forget insert."""
    import server.core.ch as ch_mod
    from server.code.app import search_cursor as paging

    calls: list[tuple[str, Any]] = []

    class _CH:
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            calls.append((sql, params))
            return {"payload": '{"pool": []}', "indexed_commit": b"abc\x00", "created_at": None}

        async def insert(self, table: str, rows: Any, settings: Any = None) -> None:
            calls.append((table, settings))

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    got = await paging.get_pool("o", "n", "cursor:s:r1")
    assert got is not None and got["payload"] == {"pool": []} and got["indexed_commit"] == "abc"
    assert (await memo.get("o", "n", "k"))["indexed_commit"] == b"abc\x00"
    for sql, params in calls[:2]:
        assert "FROM loom.code_search_memo WHERE org = {org:String}" in sql
        assert params["ttl"] == paging.ttl_s()
    paging.put_pool("o", "n", "cursor:s:r1", {"pool": []}, indexed_commit="c", route="r")
    await asyncio.gather(*list(paging._TASKS))
    assert calls[2] == ("loom.code_search_memo", {"async_insert": 1, "wait_for_async_insert": 0})
