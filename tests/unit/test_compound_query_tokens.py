"""Adjacent query words the corpus writes as one term."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval.lexical import compound_match
from server.memory.app.search.evidence import _floor_exempt
from server.memory.app.search_bm25 import compound_query_tokens, fetch_bm25_corpus_stats_probing
from server.memory.domain.search_types import SearchQuery


def _q(**kw: Any) -> SearchQuery:
    base: dict[str, Any] = dict(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.1],
        top_k=5,
        search_mode="rrf",
    )
    base.update(kw)
    return SearchQuery(**base)


def test_compounds_are_adjacent_runs_longest_first() -> None:
    words = ["clickpipes", "user", "certificate", "not", "ready"]
    out = compound_query_tokens(words)
    assert out[0] == "clickpipesusercertificatenotready"
    assert "clickpipesuser" in out and "notready" in out
    assert "clickpipescertificate" not in out  # not adjacent
    assert all(len(t) <= 48 for t in out)
    assert compound_query_tokens(["one"]) == []
    assert len(compound_query_tokens([f"w{i}" for i in range(40)], cap=10)) == 10


class _StubCH:
    def __init__(self, known: dict[str, int]) -> None:
        self.known = known
        self.asked: list[list[str]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "uniqExactMerge" in sql:
            self.asked.append(list((params or {})["tokens"]))
            return [
                {"term": t, "df": d} for t, d in self.known.items() if t in (params or {})["tokens"]
            ]
        return [{"n": 100, "avgdl": 30.0}]


@pytest.fixture(autouse=True)
def _fresh_reader_state():
    from server.memory.app import search_bm25 as m

    m._BM25_CORPUS_CACHE.clear()
    yield
    m._BM25_CORPUS_CACHE.clear()


@pytest.mark.asyncio
async def test_probing_fetch_keeps_only_compounds_the_corpus_knows() -> None:
    ch = _StubCH({"certificate": 40, "clickpipesusercertificatenotready": 3})
    final, idfs, avgdl = await fetch_bm25_corpus_stats_probing(
        ch,
        org="o",
        namespace="n",
        tokens=["certificate", "ready"],
        probes=["clickpipesusercertificatenotready", "certificateready", "ready"],
    )
    assert final == ["certificate", "ready", "clickpipesusercertificatenotready"]
    assert len(idfs) == 3 and avgdl == 30.0
    assert idfs[2] > idfs[0]  # the rare compound outweighs the common word
    assert len(ch.asked) == 1  # one df read for tokens and probes together


@pytest.mark.asyncio
async def test_probing_fetch_without_probes_matches_plain_reader() -> None:
    ch = _StubCH({"fox": 2})
    final, idfs, avgdl = await fetch_bm25_corpus_stats_probing(
        ch, org="o", namespace="n", tokens=["fox", "unseen"], probes=[]
    )
    assert final == ["fox", "unseen"] and len(idfs) == 2 and avgdl == 30.0


def test_compound_match_is_case_insensitive_substring() -> None:
    assert compound_match(
        "| ClickPipesUserCertificateNotReady | 71af |", ("clickpipesusercertificatenotready",)
    )
    assert not compound_match("| SingleProxyTestFailing |", ("clickpipesusercertificatenotready",))
    assert not compound_match("anything", ())


def test_floor_exempts_identifier_carriers() -> None:
    class _H:
        def __init__(self, comps: dict[str, float]) -> None:
            self.score_components = comps

    assert _floor_exempt(_H({"compound_match": 1.0}))
    assert _floor_exempt(_H({"identifier_match": 1.0}))
    assert not _floor_exempt(_H({"lexical": 0.9, "semantic": 0.3}))


@pytest.mark.asyncio
async def test_numeric_identifiers_reach_the_idf_arm(monkeypatch) -> None:
    """The roll-up splits on non-alphanumerics and keeps digits,
    so the query side must too — a run id or commit hash is the rarest term
    a templated report carries."""
    from server.memory.app.retrieval import lexical as ms

    seen: dict[str, list[str]] = {}

    async def probing(ch, *, org, namespace, tokens, probes, drop_if_unseen=frozenset()):
        seen["tokens"] = list(tokens)
        return list(tokens), [1.0 for _ in tokens], 100.0

    monkeypatch.setattr(ms, "fetch_bm25_corpus_stats_probing", probing)
    q = _q(
        query_text="CI failure analysis run 35229427506 commit d60ae65d5507 suite6",
        search_mode="bm25_hybrid",
    )
    out = await ms._bm25_precursor(object(), q)
    assert "35229427506" in seen["tokens"] and "d60ae65d5507" in seen["tokens"]
    assert "35229427506" in out.bm25_tokens


@pytest.mark.asyncio
async def test_lexical_pool_orders_by_the_token_count() -> None:
    """The union pool's lex_score is the plain count of query tokens present."""
    from server.memory.app.retrieval import side_legs as ms

    class _CH:
        def __init__(self) -> None:
            self.sqls: list[str] = []
            self.params: list[dict[str, Any]] = []

        async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict:
            return {"n": 0}

        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
            self.sqls.append(sql)
            self.params.append(dict(params or {}))
            return []

    ch = _CH()
    q = _q(
        query_text="denimaws cache impact",
        bm25_tokens=["denimaws", "cache", "impact"],
        bm25_idfs=[8.0, 0.5, 0.4],
        bm25_avgdl=500.0,
    )
    await ms._lexical_recall_fallback(ch, q, limit=50, max_distance=0.9)
    assert "(tok, idf)" not in ch.sqls[-1]
    assert "positionCaseInsensitive(e.content_excerpt_lc, t) > 0" in ch.sqls[-1]
