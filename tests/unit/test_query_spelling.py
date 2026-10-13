"""A lowercased word in a short query embeds in the spelling the corpus uses."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search import query_spelling as app
from server.memory.domain.query_spelling import apply_spellings, pick_spellings, spelling_candidates


def _input(query: str = "datalakecatalog") -> MemorySearchInput:
    return MemorySearchInput(
        org="o", namespace="ns", agent="*", query=query, user_id="u1", session_id="s1"
    )


@pytest.fixture(autouse=True)
def _empty_caches():
    app._cache.clear()
    app._slow_until.clear()


def _lookup_returns(monkeypatch, rows):
    async def spelling_rows(_ch, q, tokens):
        if isinstance(rows, BaseException):
            raise rows
        return rows

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)


def test_candidates_are_the_short_querys_lowercase_words():
    assert spelling_candidates("datalakecatalog", max_words=6) == ["datalakecatalog"]
    # A capital, digit or underscore is the caller's spelling; short words carry nothing.
    query = "DataLakeCatalog nessie s3 iceberg2 data_lake"
    assert spelling_candidates(query, max_words=6) == ["nessie"]
    assert spelling_candidates("kafka kafka", max_words=6) == ["kafka"]


def test_a_long_question_or_off_looks_nothing_up():
    q = "what did we decide about the datalakecatalog credentials last week"
    assert spelling_candidates(q, max_words=6) == []
    assert spelling_candidates("datalakecatalog", max_words=0) == []


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ({"DataLakeCatalog": 69}, "DataLakeCatalog"),
        ({"DataLakeCatalog": 40, "datalakecatalog": 2}, "DataLakeCatalog"),
        # One row is no evidence, and a near-even split keeps the word as typed.
        ({"DataLakeCatalog": 1}, None),
        ({"User": 116, "user": 114}, None),
        ({"GOAL": 1, "goal": 1}, None),
        ({"Deploy": 3, "deploy": 9}, None),
        # Two other spellings tie: the choice is stable.
        ({"Iceberg": 2, "ICEBERG": 2}, "Iceberg"),
    ],
)
def test_a_spelling_replaces_the_word_only_when_it_clearly_leads(rows, expected):
    word = next(iter(rows)).lower()
    assert pick_spellings([word], rows).get(word) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("datalakecatalog bugs\n2026-10-01", "DataLakeCatalog bugs\n2026-10-01"),
        ("datalakecatalogs", "datalakecatalogs"),
        ("datalakecatalog2", "datalakecatalog2"),
        ("datalakecatalog_suffix", "datalakecatalog_suffix"),
        ("x.datalakecatalog", "x.DataLakeCatalog"),
    ],
)
def test_only_whole_words_are_rewritten(text, expected):
    assert apply_spellings(text, {"datalakecatalog": "DataLakeCatalog"}) == expected


def test_the_lookup_reads_with_the_callers_bounds(monkeypatch):
    seen = {}

    async def spelling_rows(_ch, q, tokens):
        seen.update(user_id=q.user_id, session_id=q.session_id, tokens=tokens)
        return {"DataLakeCatalog": 7}

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    assert asyncio.run(app.corpus_spellings(_input(), "m")) == {
        "datalakecatalog": "DataLakeCatalog"
    }
    assert seen == {"user_id": "u1", "session_id": "s1", "tokens": ["datalakecatalog"]}


def test_the_deadline_reaches_clickhouse(monkeypatch):
    from server.core.ch import statement_timeout_var

    async def spelling_rows(_ch, _q, _tokens):
        assert statement_timeout_var.get() == app._LOOKUP_BOUND_S
        return {}

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    assert asyncio.run(app.corpus_spellings(_input(), "m")) == {}


def test_a_failed_lookup_embeds_as_typed_and_backs_off(monkeypatch):
    calls = []

    async def spelling_rows(_ch, _q, _tokens):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    assert asyncio.run(app.corpus_spellings(_input(), "m")) is None
    # The namespace is not read again for a minute, whatever the query.
    assert asyncio.run(app.corpus_spellings(_input("iceberg"), "m")) is None
    assert len(calls) == 1
    clock = time.monotonic() + app._CACHE_TTL_S + 1
    monkeypatch.setattr(app.time, "monotonic", lambda: clock)
    asyncio.run(app.corpus_spellings(_input(), "m"))
    assert len(calls) == 2


def test_a_failed_lookup_is_not_a_provider_outage(monkeypatch):
    from server.observability import provider_health

    _lookup_returns(monkeypatch, TimeoutError())
    asyncio.run(app.corpus_spellings(_input(), "m"))
    assert "query_spelling" not in provider_health.status()["providers"]


def test_an_answer_is_reused_for_the_same_bounds_and_words(monkeypatch):
    calls = []

    async def spelling_rows(_ch, q, _tokens):
        calls.append(q.user_id)
        return {"DataLakeCatalog": 7}

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    for _ in range(3):
        asyncio.run(app.corpus_spellings(_input(), "m"))
    other_user = _input().model_copy(update={"user_id": "u2"})
    asyncio.run(app.corpus_spellings(other_user, "m"))
    assert calls == ["u1", "u2"]


def test_memory_search_query_spelling_max_words_zero_reads_nothing(monkeypatch):
    from server.settings import settings

    _lookup_returns(monkeypatch, AssertionError("no lookup when off"))
    monkeypatch.setattr(settings, "memory_search_query_spelling_max_words", 0)
    assert asyncio.run(app.corpus_spellings(_input(), "m")) == {}


class _Embedder:
    def __init__(self, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> list[float]:
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("embedder down")
        return [float(len(self.texts))]


def test_no_rewrite_uses_the_one_embed_started_beside_the_lookup(monkeypatch):
    _lookup_returns(monkeypatch, {})
    embed = _Embedder()
    vec, spellings = asyncio.run(app.embed_query(embed, "datalakecatalog 2026", _input(), "m"))
    assert (vec, spellings, embed.texts) == ([1.0], {}, ["datalakecatalog 2026"])


def test_a_rewrite_embeds_the_corpus_spelling(monkeypatch):
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})
    embed = _Embedder()
    vec, spellings = asyncio.run(app.embed_query(embed, "datalakecatalog 2026", _input(), "m"))
    assert spellings == {"datalakecatalog": "DataLakeCatalog"}
    assert embed.texts[-1] == "DataLakeCatalog 2026"
    assert vec == [float(len(embed.texts))]


def test_an_embed_failure_still_fails_the_search(monkeypatch):
    _lookup_returns(monkeypatch, {})
    with pytest.raises(RuntimeError, match="embedder down"):
        asyncio.run(app.embed_query(_Embedder(fail=True), "datalakecatalog", _input(), "m"))


def _ch_client(query: Any) -> Any:
    """A real CHClient (slot gate, retry ladder) over a fake driver `query`."""
    import server.core.ch as ch_mod

    c = ch_mod.CHClient.__new__(ch_mod.CHClient)
    c._client = SimpleNamespace(query=query)
    return c


async def _no_rows(*_a: Any, **_kw: Any) -> Any:
    return SimpleNamespace(column_names=["spelling", "n"], result_rows=[])


async def _embed_beside(monkeypatch: Any, client: Any) -> tuple[list[float], dict[str, str], float]:
    monkeypatch.setattr(app, "get_client", lambda: client)
    t = time.monotonic()
    vec, spellings = await app.embed_query(_Embedder(), "datalakecatalog", _input(), "m")
    return vec, spellings, time.monotonic() - t


async def test_a_busy_pool_does_not_hold_back_the_embed(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_pool_wait_ms", 5000)
    client = _ch_client(_no_rows)
    client._ensure_state()
    client._slots = asyncio.Semaphore(1)
    await client._slots.acquire()  # every connection is busy
    vec, spellings, took = await _embed_beside(monkeypatch, client)
    assert (vec, spellings) == ([1.0], None)
    assert took < app._LOOKUP_BOUND_S + 0.2


async def test_a_slow_reconnect_does_not_hold_back_the_embed(monkeypatch):
    async def dropped(*_a: Any, **_kw: Any) -> Any:
        raise ConnectionResetError("peer closed")

    client = _ch_client(dropped)

    async def slow_connect() -> None:
        await asyncio.sleep(5)

    client.connect = slow_connect
    vec, spellings, took = await _embed_beside(monkeypatch, client)
    assert (vec, spellings) == ([1.0], None)
    assert took < app._LOOKUP_BOUND_S + 0.2
