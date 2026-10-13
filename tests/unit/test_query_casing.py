"""A lowercased word in a short query embeds in the spelling the corpus uses."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.memory.app.search import query_casing as app
from server.memory.domain.query_casing import apply_spellings, casing_candidates, pick_spellings


def _input(query: str, **kw: object) -> SimpleNamespace:
    fields = {
        "org": "o",
        "namespace": "ns",
        "scope": "private",
        "agent": "*",
        "query": query,
        "memory_type": "",
        "user_id": "u1",
        "session_id": "s1",
        "session_ids": ["s1"],
        "run_id": None,
        "as_of": None,
    }
    return SimpleNamespace(**{**fields, **kw})


def test_candidates_are_the_short_querys_lowercase_words():
    assert casing_candidates("datalakecatalog", max_words=6) == ["datalakecatalog"]
    # A capital, digit or underscore is the caller's spelling; short words carry nothing.
    query = "DataLakeCatalog nessie s3 iceberg2 data_lake"
    assert casing_candidates(query, max_words=6) == ["nessie"]
    assert casing_candidates("kafka kafka", max_words=6) == ["kafka"]


def test_a_long_question_or_off_looks_nothing_up():
    q = "what did we decide about the datalakecatalog credentials last week"
    assert casing_candidates(q, max_words=6) == []
    assert casing_candidates("datalakecatalog", max_words=0) == []


def test_the_most_frequent_spelling_wins_and_lowercase_stays():
    counts = {"DataLakeCatalog": 40, "datalakecatalog": 2, "Deploy": 3, "deploy": 9}
    assert pick_spellings(["datalakecatalog", "deploy", "absent"], counts) == {
        "datalakecatalog": "DataLakeCatalog"
    }
    assert pick_spellings(["iceberg"], {"Iceberg": 2, "ICEBERG": 2}) == {"iceberg": "ICEBERG"}


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

    async def counts(_ch, q, tokens):
        seen.update(user_id=q.user_id, session_id=q.session_id, session_ids=q.session_ids)
        assert tokens == ["datalakecatalog"]
        return {"DataLakeCatalog": 7}

    monkeypatch.setattr(app, "spelling_counts", counts)
    monkeypatch.setattr(app, "get_client", lambda: None)
    got = asyncio.run(app.corpus_spellings(_input("datalakecatalog"), "m"))
    assert got == {"datalakecatalog": "DataLakeCatalog"}
    assert seen == {"user_id": "u1", "session_id": "s1", "session_ids": ["s1"]}


def test_the_deadline_reaches_clickhouse(monkeypatch):
    from server.core.ch import statement_timeout_var

    async def counts(_ch, _q, _tokens):
        assert statement_timeout_var.get() == app._LOOKUP_BOUND_S
        return {}

    monkeypatch.setattr(app, "spelling_counts", counts)
    monkeypatch.setattr(app, "get_client", lambda: None)
    assert asyncio.run(app.corpus_spellings(_input("datalakecatalog"), "m")) == {}


@pytest.mark.parametrize("failure", ["raise", "slow"])
def test_a_failed_or_slow_lookup_embeds_the_query_as_typed(monkeypatch, failure):
    async def counts(_ch, _q, _tokens):
        if failure == "raise":
            raise RuntimeError("ch down")
        await asyncio.sleep(5)
        return {"DataLakeCatalog": 1}

    monkeypatch.setattr(app, "spelling_counts", counts)
    monkeypatch.setattr(app, "get_client", lambda: None)
    monkeypatch.setattr(app, "_LOOKUP_BOUND_S", 0.05)
    assert asyncio.run(app.corpus_spellings(_input("datalakecatalog"), "m")) == {}


def test_memory_search_corpus_casing_max_words_zero_reads_nothing(monkeypatch):
    from server.settings import settings

    async def counts(_ch, _q, _tokens):
        raise AssertionError("no lookup when off")

    monkeypatch.setattr(app, "spelling_counts", counts)
    monkeypatch.setattr(settings, "memory_search_corpus_casing_max_words", 0)
    assert asyncio.run(app.corpus_spellings(_input("datalakecatalog"), "m")) == {}
