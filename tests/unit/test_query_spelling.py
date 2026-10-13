"""A lowercased word in a short query embeds in the spelling the corpus uses."""

from __future__ import annotations

import asyncio
import logging
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


async def _spellings(inp: MemorySearchInput) -> dict[str, str] | None:
    """What a search with `inp` respells, through `embed_query` itself."""

    async def embed(_text: str) -> list[float]:
        return [1.0]

    return (await app.embed_query(embed, inp.query, inp, "m"))[1]


@pytest.fixture(autouse=True)
def _empty_caches():
    app._cache.clear()
    app._failed.clear()


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
        # A capital that only opens a sentence or a name is not an identifier.
        ({"What": 349, "what": 153, "WHAT": 109}, None),
        ({"However": 29, "however": 3}, None),
        ({"Kafka": 42, "kafka": 6}, None),
        # Counted against the plain spellings together.
        ({"FINAL": 146, "final": 53}, "FINAL"),
        ({"ROOT": 400, "Root": 446, "root": 251}, None),
        # Two identifier spellings tie: the choice is stable.
        ({"IceBerg": 2, "ICEBERG": 2}, "IceBerg"),
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
    assert asyncio.run(_spellings(_input())) == {"datalakecatalog": "DataLakeCatalog"}
    assert seen == {"user_id": "u1", "session_id": "s1", "tokens": ["datalakecatalog"]}


def test_the_deadline_reaches_clickhouse(monkeypatch):
    from server.core.ch import statement_timeout_var

    async def spelling_rows(_ch, _q, _tokens):
        assert statement_timeout_var.get() == app._LOOKUP_BOUND_S
        return {}

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    assert asyncio.run(_spellings(_input())) == {}


def test_a_failing_namespace_backs_off_longer_each_time_and_warns_once(monkeypatch, caplog):
    calls = []
    clock = [1000.0]

    async def spelling_rows(_ch, _q, _tokens):
        calls.append(clock[0])
        raise TimeoutError()

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.DEBUG, logger=app.log.name):
        for _ in range(4):
            assert asyncio.run(_spellings(_input())) is None
            # Another query in the namespace waits out the same back-off.
            assert asyncio.run(_spellings(_input("iceberg"))) is None
            clock[0] = app._failed[("o", "ns")][1] + 1  # wait out the back-off
    assert [round(b - a) for a, b in zip(calls, calls[1:], strict=False)] == [61, 121, 241]
    assert [r.levelno for r in caplog.records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
        logging.DEBUG,
    ]


def test_a_success_ends_the_back_off(monkeypatch):
    _lookup_returns(monkeypatch, TimeoutError())
    asyncio.run(_spellings(_input()))
    app._failed[("o", "ns")] = (3, 0.0)  # its back-off has run out
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 7})
    assert asyncio.run(_spellings(_input())) == {"datalakecatalog": "DataLakeCatalog"}
    assert ("o", "ns") not in app._failed


def test_a_failed_lookup_is_not_a_provider_outage(monkeypatch):
    from server.observability import provider_health

    _lookup_returns(monkeypatch, TimeoutError())
    asyncio.run(_spellings(_input()))
    assert "query_spelling" not in provider_health.status()["providers"]


def test_an_answer_is_reused_for_the_same_bounds_and_words(monkeypatch):
    calls = []

    async def spelling_rows(_ch, q, _tokens):
        calls.append(q.user_id)
        return {"DataLakeCatalog": 7}

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    for _ in range(3):
        asyncio.run(_spellings(_input()))
    other_user = _input().model_copy(update={"user_id": "u2"})
    asyncio.run(_spellings(other_user))
    assert calls == ["u1", "u2"]


def test_memory_search_query_spelling_max_words_zero_reads_nothing(monkeypatch):
    from server.settings import settings

    _lookup_returns(monkeypatch, AssertionError("no lookup when off"))
    monkeypatch.setattr(settings, "memory_search_query_spelling_max_words", 0)
    assert asyncio.run(_spellings(_input())) == {}


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


async def test_a_failed_rewrite_embed_serves_the_query_as_typed(monkeypatch):
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})

    async def embed(text: str) -> list[float]:
        if text == "DataLakeCatalog":
            raise TimeoutError("provider slow")
        return [1.0]

    assert await app.embed_query(embed, "datalakecatalog", _input(), "m") == ([1.0], None)


async def test_lookups_failing_together_count_and_warn_once(monkeypatch, caplog):
    async def spelling_rows(_ch, _q, _tokens):
        await asyncio.sleep(0.05)
        raise TimeoutError()

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    queries = [f"datalakecatalog word{i:04d}" for i in range(20)]
    with caplog.at_level(logging.DEBUG, logger=app.log.name):
        got = await asyncio.gather(*(_spellings(_input(q)) for q in queries))
    assert got == [None] * 20
    assert app._failed[("o", "ns")][0] == 1
    assert len(caplog.records) == 1


class _FakeEmbedder:
    model_name = "m"

    async def embed(self, text: str) -> list[float]:
        return [1.0]


@pytest.mark.parametrize(
    ("spellings", "work"),
    [
        (
            {"datalakecatalog": "DataLakeCatalog"},
            {"embedding_calls": 2, "query_spellings": {"datalakecatalog": "DataLakeCatalog"}},
        ),
        ({}, {"embedding_calls": 1}),
        (None, {"embedding_calls": 1, "query_spellings_skipped": True}),
    ],
)
async def test_memory_search_reports_the_spelling_in_retrieval_work(monkeypatch, spellings, work):
    from server.memory.app.search.embed import _embed_search_query

    async def lookup(_q, _tokens, _key):
        await asyncio.sleep(0)  # a real lookup waits on ClickHouse, so the as-typed embed starts
        return spellings

    monkeypatch.setattr(app, "_lookup", lookup)
    actual_work: dict[str, Any] = {"embedding_calls": 0}
    clock = SimpleNamespace(mark=lambda _stage: None)
    no_hyde = _input().model_copy(update={"hyde": False})
    vec = await _embed_search_query(
        no_hyde, _FakeEmbedder(), "datalakecatalog", "", None, actual_work, clock
    )
    assert vec == [1.0]
    assert actual_work == work


@pytest.mark.parametrize(
    ("spellings", "reported", "skipped"),
    [
        ({"datalakecatalog": "DataLakeCatalog"}, {"datalakecatalog": "DataLakeCatalog"}, False),
        ({}, {}, False),
        (None, {}, True),
    ],
)
async def test_memory_search_trace_reports_the_spelling(monkeypatch, spellings, reported, skipped):
    import server.memory.app.retrieval as retrieval
    from server.memory.api.models_search import MemorySearchTraceInput
    from server.memory.app.search import trace

    async def lookup(_q, _tokens, _key):
        return spellings

    async def no_hits(_q):
        return []

    monkeypatch.setattr(app, "_lookup", lookup)
    monkeypatch.setattr(trace, "get_embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(retrieval, "vector_search_trace", no_hits)
    out = await trace.handle_memory_search_trace(
        MemorySearchTraceInput(org="o", namespace="ns", agent="*", query="datalakecatalog")
    )
    assert (out.query_spellings, out.query_spellings_skipped) == (reported, skipped)


def test_the_back_off_map_stays_bounded_and_forgets_ended_streaks(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    _lookup_returns(monkeypatch, TimeoutError())
    for i in range(app._FAILED_SIZE + 100):
        asyncio.run(_spellings(_input().model_copy(update={"namespace": f"ns{i}"})))
    assert len(app._failed) == app._FAILED_SIZE
    assert ("o", "ns0") not in app._failed  # the oldest went first
    # An hour after their back-offs end, the next failure clears them all.
    clock[0] += app._CACHE_TTL_S + app._MAX_BACKOFF_S + 1
    asyncio.run(_spellings(_input().model_copy(update={"namespace": "late"})))
    assert list(app._failed) == [("o", "late")]


async def test_an_embed_that_raises_before_awaiting_leaves_no_task_running(monkeypatch):
    started = asyncio.Event()

    async def spelling_rows(_ch, _q, _tokens):
        started.set()
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)

    def embed(_text: str) -> Any:
        raise RuntimeError("no embedder")

    before = asyncio.all_tasks()
    with pytest.raises(RuntimeError, match="no embedder"):
        await app.embed_query(embed, "datalakecatalog", _input(), "m")
    assert asyncio.all_tasks() == before


async def test_a_failed_rewrite_embed_still_counts_both_calls(monkeypatch):
    from server.memory.app.search.embed import _embed_search_query

    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})

    class Flaky(_FakeEmbedder):
        async def embed(self, text: str) -> list[float]:
            if text == "DataLakeCatalog":
                raise RuntimeError("provider error")
            return [1.0]

    actual_work: dict[str, Any] = {"embedding_calls": 0}
    clock = SimpleNamespace(mark=lambda _stage: None)
    no_hyde = _input().model_copy(update={"hyde": False})
    vec = await _embed_search_query(
        no_hyde, Flaky(), "datalakecatalog", "", None, actual_work, clock
    )
    assert vec == [1.0]
    assert actual_work == {"embedding_calls": 2, "query_spellings_skipped": True}


def test_a_cache_hit_keeps_the_answer_from_being_evicted_first(monkeypatch):
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 7})
    monkeypatch.setattr(app, "_CACHE_SIZE", 2)
    first, second, third = (_input(q) for q in ("datalakecatalog", "iceberg", "parquet"))
    for inp in (first, second, first, third):
        asyncio.run(_spellings(inp))
    assert [k[1] for k in app._cache] == [("datalakecatalog",), ("parquet",)]


async def test_a_dead_embedder_fails_the_search_without_claiming_a_fallback(monkeypatch, caplog):
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})
    texts: list[str] = []

    async def embed(text: str) -> list[float]:
        texts.append(text)
        await asyncio.sleep(0.05)
        raise RuntimeError("401 from the provider")

    with (
        caplog.at_level(logging.WARNING, logger=app.log.name),
        pytest.raises(RuntimeError, match="401"),
    ):
        await app.embed_query(embed, "datalakecatalog", _input(), "m")
    # Both texts were tried before the search failed, and no fallback was announced.
    assert sorted(texts) == ["DataLakeCatalog", "datalakecatalog"]
    assert not caplog.records


async def test_a_cached_answer_skips_the_lookup(monkeypatch):
    calls = []

    async def spelling_rows(_ch, _q, _tokens):
        calls.append(1)
        return {"DataLakeCatalog": 9}

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)
    for _ in range(3):
        embed = _Embedder()
        _vec, spellings = await app.embed_query(embed, "datalakecatalog", _input(), "m")
        assert spellings == {"datalakecatalog": "DataLakeCatalog"}
        assert embed.texts[-1] == "DataLakeCatalog"
    assert calls == [1]


async def test_a_cached_no_rewrite_embeds_once(monkeypatch):
    _lookup_returns(monkeypatch, {})
    await _spellings(_input())
    embed = _Embedder()
    assert await app.embed_query(embed, "datalakecatalog", _input(), "m") == ([1.0], {})
    assert embed.texts == ["datalakecatalog"]


async def test_an_outage_serves_the_query_as_typed_cached_or_not(monkeypatch):
    # The embedding cache answers the text it has seen; the provider is down.
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})

    async def embed(text: str) -> list[float]:
        if text == "datalakecatalog":
            return [1.0]
        raise RuntimeError("provider down")

    for _ in range(2):  # the first call looks the spelling up, the second has it cached
        assert await app.embed_query(embed, "datalakecatalog", _input(), "m") == ([1.0], None)


async def test_a_dead_embedder_fails_once_the_lookup_has_answered(monkeypatch):
    async def spelling_rows(_ch, _q, _tokens):
        await asyncio.sleep(5)  # past the lookup's deadline
        return {}

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)

    async def embed(_text: str) -> list[float]:
        raise RuntimeError("401 from the provider")

    t = time.monotonic()
    with pytest.raises(RuntimeError, match="401"):
        await app.embed_query(embed, "datalakecatalog", _input(), "m")
    assert time.monotonic() - t < app._LOOKUP_BOUND_S + 0.2


async def test_a_cached_rewrite_runs_one_embed(monkeypatch):
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})
    await _spellings(_input())  # warms the cache
    embed = _Embedder()
    assert await app.embed_query(embed, "datalakecatalog", _input(), "m") == (
        [1.0],
        {"datalakecatalog": "DataLakeCatalog"},
    )
    assert embed.texts == ["DataLakeCatalog"]


async def test_an_outage_with_only_the_respelled_text_cached_serves_it_cached_or_not(monkeypatch):
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})

    async def embed(text: str) -> list[float]:
        if text == "DataLakeCatalog":
            return [2.0]
        raise RuntimeError("provider down")

    for _ in range(2):  # the first call looks the spelling up, the second has it cached
        assert await app.embed_query(embed, "datalakecatalog", _input(), "m") == (
            [2.0],
            {"datalakecatalog": "DataLakeCatalog"},
        )


class _Hung:
    """A provider that hangs on every text but those the embedding cache holds."""

    def __init__(self, cached: dict[str, list[float]]) -> None:
        self.cached = cached
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        if text in self.cached:
            return self.cached[text]
        await asyncio.sleep(60)
        return [0.0]


@pytest.fixture
def short_bound(monkeypatch):
    from server.settings import settings

    # Longer than the lookup's bound, as in production (2.5 s against 0.25 s).
    monkeypatch.setattr(settings, "memory_search_embed_bound_s", 0.3)
    monkeypatch.setattr(settings, "memory_search_embed_bound_retries", 0)


async def _embed_with(
    provider: _Hung, work: dict[str, Any] | None = None
) -> tuple[list[float], dict[str, str] | None]:
    from server.memory.app.search.embed import _bounded_query_embed

    return await app.embed_query(
        lambda text: _bounded_query_embed(provider, text, work), "datalakecatalog", _input(), "m"
    )


async def test_a_hung_provider_answers_embed_timeout_without_a_second_bounded_wait(
    monkeypatch, short_bound
):
    from server.core.errors import EmbedTimeoutError

    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})
    await _spellings(_input())  # the rewrite is cached
    provider = _Hung({})
    work: dict[str, Any] = {}
    with pytest.raises(EmbedTimeoutError):
        await _embed_with(provider, work)
    # The bounded attempt at the respelled text, then a try as typed cut at the lookup's bound.
    assert provider.texts == ["DataLakeCatalog", "datalakecatalog"]
    # Only the first ran out its own bound; the cut try never reached its timeout.
    assert work["embed_timeouts"] == 1


@pytest.mark.parametrize("warm", [False, True])
async def test_a_hung_provider_still_serves_the_text_as_typed_from_the_embedding_cache(
    monkeypatch, short_bound, warm
):
    _lookup_returns(monkeypatch, {"DataLakeCatalog": 9})
    if warm:
        await _spellings(_input())
    assert await _embed_with(_Hung({"datalakecatalog": [1.0]})) == ([1.0], None)


def test_a_namespace_failing_for_weeks_stays_at_the_hour_and_never_fails_the_search(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    _lookup_returns(monkeypatch, TimeoutError())
    for _ in range(1100):  # past 2**1024, where an uncapped exponent overflows a float
        failed_at = clock[0]
        assert asyncio.run(_spellings(_input())) is None
        clock[0] = app._failed[("o", "ns")][1] + 1
    failures, retry_at = app._failed[("o", "ns")]
    assert failures == 1100 and retry_at - failed_at == app._MAX_BACKOFF_S


def test_a_namespace_whose_streak_ended_long_ago_starts_a_new_one(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    _lookup_returns(monkeypatch, TimeoutError())
    app._failed[("o", "ns")] = (4, clock[0] - app._MAX_BACKOFF_S - 1)  # ended over an hour ago
    with caplog.at_level(logging.WARNING, logger=app.log.name):
        assert asyncio.run(_spellings(_input())) is None
    assert app._failed[("o", "ns")] == (1, clock[0] + app._CACHE_TTL_S)
    assert len(caplog.records) == 1  # a new streak warns again
