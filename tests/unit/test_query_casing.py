"""A lowercased word in a short query embeds in the spelling the corpus uses."""

from __future__ import annotations

import asyncio

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search import query_casing as app
from server.memory.domain.query_casing import apply_spellings, casing_candidates, pick_spellings


def _input(query: str = "datalakecatalog") -> MemorySearchInput:
    return MemorySearchInput(
        org="o", namespace="ns", agent="*", query=query, user_id="u1", session_id="s1"
    )


def _lookup_returns(monkeypatch, rows):
    async def spelling_rows(_ch, q, tokens):
        if isinstance(rows, BaseException):
            raise rows
        return rows

    monkeypatch.setattr(app, "spelling_rows", spelling_rows)
    monkeypatch.setattr(app, "get_client", lambda: None)


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


def test_a_failed_lookup_embeds_as_typed_and_says_so(monkeypatch):
    noted = []
    monkeypatch.setattr(app.degradations, "note", lambda *a: noted.append(a[:2]))
    _lookup_returns(monkeypatch, TimeoutError())
    assert asyncio.run(app.corpus_spellings(_input(), "m")) == {}
    assert noted == [("query_spelling", "embedded_as_typed")]


def test_memory_search_corpus_casing_max_words_zero_reads_nothing(monkeypatch):
    from server.settings import settings

    _lookup_returns(monkeypatch, AssertionError("no lookup when off"))
    monkeypatch.setattr(settings, "memory_search_corpus_casing_max_words", 0)
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
