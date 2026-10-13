"""A lowercased identifier in a short query embeds in the namespace's own spelling."""

from __future__ import annotations

import asyncio

import pytest

from server.memory.app.search import query_casing as app
from server.memory.domain.query_casing import apply_spellings, casing_candidates, pick_spellings


def test_candidates_are_the_short_querys_lowercase_words():
    assert casing_candidates("datalakecatalog", max_words=6) == ["datalakecatalog"]
    # A capital is the caller's spelling; short words carry nothing.
    assert casing_candidates("DataLakeCatalog the nessie s3", max_words=6) == ["nessie"]
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
    # A tie is broken the same way every time.
    assert pick_spellings(["iceberg"], {"Iceberg": 2, "ICEBERG": 2}) == {"iceberg": "ICEBERG"}


def test_only_whole_words_are_rewritten():
    sp = {"datalakecatalog": "DataLakeCatalog"}
    assert (
        apply_spellings("datalakecatalog bugs\n2026-10-01", sp)
        == "DataLakeCatalog bugs\n2026-10-01"
    )
    assert apply_spellings("datalakecatalogs", sp) == "datalakecatalogs"
    assert apply_spellings("x", {}) == "x"


def test_the_lookup_returns_the_corpus_spelling(monkeypatch):
    async def counts(_ch, *, org, namespace, tokens):
        assert (org, namespace, tokens) == ("o", "ns", ["datalakecatalog"])
        return {"DataLakeCatalog": 7}

    monkeypatch.setattr(app, "spelling_counts", counts)
    monkeypatch.setattr(app, "get_client", lambda: None)
    got = asyncio.run(app.corpus_spellings("o", "ns", "datalakecatalog"))
    assert got == {"datalakecatalog": "DataLakeCatalog"}


@pytest.mark.parametrize("failure", ["raise", "slow"])
def test_a_failed_or_slow_lookup_embeds_the_query_as_typed(monkeypatch, failure):
    async def counts(_ch, **_kw):
        if failure == "raise":
            raise RuntimeError("ch down")
        await asyncio.sleep(5)
        return {"DataLakeCatalog": 1}

    monkeypatch.setattr(app, "spelling_counts", counts)
    monkeypatch.setattr(app, "get_client", lambda: None)
    monkeypatch.setattr(app, "_LOOKUP_BOUND_S", 0.05)
    assert asyncio.run(app.corpus_spellings("o", "ns", "datalakecatalog")) == {}


def test_memory_search_corpus_casing_max_words_zero_reads_nothing(monkeypatch):
    from server.settings import settings

    async def counts(_ch, **_kw):
        raise AssertionError("no lookup when off")

    monkeypatch.setattr(app, "spelling_counts", counts)
    monkeypatch.setattr(settings, "memory_search_corpus_casing_max_words", 0)
    assert asyncio.run(app.corpus_spellings("o", "ns", "datalakecatalog")) == {}
