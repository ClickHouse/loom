"""The shared BM25 module: the query tokenizer mirrors splitByNonAlpha, one IDF,
one SQL sum, and the planes that use them (memory's stem rule, the knowledge
corpus in words, the fail-open stats reads that now say they failed)."""

from __future__ import annotations

import logging
import math
from typing import Any

import pytest

from server.core import bm25


def test_query_tokens_keep_digits_like_split_by_non_alpha() -> None:
    # splitByNonAlpha('s3bucket v2api ipv6 k8s') returns all four intact
    assert bm25.query_tokens("s3bucket v2api ipv6 k8s", min_len=3) == [
        "s3bucket",
        "v2api",
        "ipv6",
        "k8s",
    ]
    assert bm25.query_tokens("Run 8812345 at v1.2.3", min_len=3) == ["run", "8812345"]


def test_query_tokens_dedup_in_first_seen_order() -> None:
    assert bm25.query_tokens("K8s pod k8s POD pod-k8s", min_len=3) == ["k8s", "pod"]


def test_query_tokens_minimum_length_is_bytes() -> None:
    assert bm25.query_tokens("a bb ccc dddd", min_len=3) == ["ccc", "dddd"]
    assert bm25.query_tokens("a bb ccc dddd", min_len=4) == ["dddd"]
    # length() counts bytes: a two-letter word with one accented letter is 3
    assert bm25.query_tokens("né", min_len=3) == ["né"]


def test_query_tokens_split_only_on_ascii_whitespace_and_punctuation() -> None:
    assert bm25.query_tokens("snake_case/path-name.txt", min_len=3) == [
        "snake",
        "case",
        "path",
        "name",
        "txt",
    ]
    # non-ASCII punctuation and `~` are inside a token for splitByNonAlpha too
    assert bm25.query_tokens("x{y}z~w`q|r@t", min_len=3) == ["z~w"]
    assert bm25.query_tokens("deploy—rollback", min_len=3) == ["deploy—rollback"]
    assert bm25.query_tokens("", min_len=3) == [] and bm25.query_tokens("--- !!", min_len=3) == []


def test_idf_and_term_shapes() -> None:
    assert bm25.idf(1, 100) > bm25.idf(50, 100) > bm25.idf(100, 100) >= 0.0
    assert math.isclose(bm25.idf(0, 10), math.log(1 + 10.5 / 0.5))
    one = bm25.term(1, 2.0, dl=100, avgdl=100.0)
    assert 0 < one < 2.0 * (bm25.K1 + 1)
    assert bm25.term(3, 2.0, dl=100, avgdl=100.0) > one  # tf saturates but grows
    assert bm25.term(1, 2.0, dl=400, avgdl=100.0) < one  # long document penalised
    assert bm25.term(0, 2.0, dl=100, avgdl=100.0) == 0.0
    # b = 1 and an empty document: 0, not 0/0
    assert bm25.term(0, 2.0, dl=0, avgdl=10.0, b=1.0) == 0.0


def test_sum_sql_shape() -> None:
    tfs = bm25.tf_array_sql("countEqual(words, tok)", "{toks:Array(String)}")
    assert tfs == "arrayMap(tok -> countEqual(words, tok), {toks:Array(String)})"
    sql = bm25.bm25_sum_sql(tfs, "{idfs:Array(Float32)}", dl="length(words)", avgdl="{a:Float32}")
    assert sql == (
        "arraySum(arrayMap((tf, idf) -> idf * tf * (1.2 + 1) / greatest("
        "tf + 1.2 * (1 - 0.75 + 0.75 * length(words) / {a:Float32}), 1.0), "
        "arrayMap(tok -> countEqual(words, tok), {toks:Array(String)}), {idfs:Array(Float32)}))"
    )


# ---- memory: the stem rule ---------------------------------------------------


def test_stems_of_raw_tokens_names_only_derived_lemmas() -> None:
    from server.memory.app.retrieval.lexical import stems_of_raw_tokens

    raw = bm25.query_tokens("running deploys k8s", min_len=3)
    lemmas = ["runn", "deploy", "k8s", "other"]
    # runn / deploy come from raw words; k8s is raw itself; other has no raw word
    assert stems_of_raw_tokens(lemmas, raw) == frozenset({"runn", "deploy"})


class _StubCH:
    def __init__(self, known: dict[str, int]) -> None:
        self.known = known

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "uniqExactMerge" in sql:
            toks = (params or {})["tokens"]
            return [{"term": t, "df": d} for t, d in self.known.items() if t in toks]
        return [{"n": 100, "avgdl": 30.0}]


@pytest.fixture
def _fresh_memory_cache():
    from server.memory.app import search_bm25 as m

    m._BM25_CORPUS_CACHE.clear()
    yield
    m._BM25_CORPUS_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_memory_cache")
async def test_unseen_stem_is_dropped_and_the_raw_word_kept() -> None:
    from server.memory.app.search_bm25 import fetch_bm25_corpus_stats_probing

    ch = _StubCH({"running": 4, "deploy": 9, "deploys": 3})
    final, idfs, _ = await fetch_bm25_corpus_stats_probing(
        ch,
        org="o",
        namespace="n",
        tokens=["runn", "deploy", "running", "deploys", "unseenraw"],
        probes=[],
        drop_if_unseen=frozenset({"runn", "deploy"}),
    )
    # runn (df 0) goes; deploy (seen) stays; an unseen raw token is kept
    assert final == ["deploy", "running", "deploys", "unseenraw"]
    assert len(idfs) == 4
    assert idfs[1] == pytest.approx(bm25.idf(4, 100))


@pytest.mark.asyncio
async def test_precursor_sends_raw_tokens_and_drops_the_unseen_stem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.retrieval import lexical as ms
    from server.memory.domain.search_types import SearchQuery

    seen: dict[str, Any] = {}

    async def _probe(ch: Any, **kw: Any) -> tuple[list[str], list[float], float]:
        seen.update(kw)
        return list(kw["tokens"]), [1.0] * len(kw["tokens"]), 10.0

    monkeypatch.setattr(ms, "fetch_bm25_corpus_stats_probing", _probe)
    q = SearchQuery(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.1],
        top_k=5,
        search_mode="bm25_hybrid",
        query_text="running on k8s",
    )
    await ms._bm25_precursor(object(), q)  # type: ignore[arg-type]
    assert "running" in seen["tokens"] and "k8s" in seen["tokens"]
    assert "runn" in seen["drop_if_unseen"] and "running" not in seen["drop_if_unseen"]


# ---- one length floor for the df views and the query side ---------------------


def _schema_view_floors(view: str) -> set[int]:
    """The `length(t) >= N` literals in the schema's CREATE statement of `view`."""
    import pathlib
    import re

    text = (pathlib.Path(__file__).resolve().parents[2] / "schemas" / "schema.sql").read_text()
    m = re.search(rf"CREATE MATERIALIZED VIEW IF NOT EXISTS loom\.{view}\b(.*?);", text, re.S)
    assert m, view
    return {int(n) for n in re.findall(r"length\(t\) >= (\d+)", m.group(1))}


def test_query_token_floor_equals_the_df_floor() -> None:
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.memory.app import search_bm25

    # the schema's term-frequency views and the SQL built in code spell one floor
    assert _schema_view_floors("memory_term_freq_refresh") == {bm25.MIN_TERM_LEN}
    assert _schema_view_floors("chunk_term_freq_mv") == {bm25.MIN_TERM_LEN}
    assert _schema_view_floors("chunk_context_term_freq_mv") == {bm25.MIN_TERM_LEN}
    assert f"length(t) >= {bm25.MIN_TERM_LEN}," in search_bm25._BM25_TERMS
    # the query sides read the same constant
    assert knowledge_bm25.MIN_TOKEN_LEN == bm25.MIN_TERM_LEN
    short = "x" * (bm25.MIN_TERM_LEN - 1)
    assert bm25.floor_tokens([short, short + "x"]) == [short + "x"]
    assert all(
        len(t.encode()) >= bm25.MIN_TERM_LEN
        for t in search_bm25.compound_query_tokens(["a", "b", "cd", "e"])
    )


@pytest.mark.asyncio
async def test_precursor_drops_a_two_byte_lemma_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.retrieval import lexical as ms
    from server.memory.domain.lemmatize import lemma_tokens
    from server.memory.domain.search_types import SearchQuery

    # the lemmatizer itself keeps two-letter words
    assert "to" in lemma_tokens("what did she go to see") and "up" in lemma_tokens("set up")
    seen: dict[str, Any] = {}

    async def _probe(ch: Any, **kw: Any) -> tuple[list[str], list[float], float]:
        seen.update(kw)
        return list(kw["tokens"]), [1.0] * len(kw["tokens"]), 10.0

    monkeypatch.setattr(ms, "fetch_bm25_corpus_stats_probing", _probe)
    q = SearchQuery(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.1],
        top_k=5,
        search_mode="bm25_hybrid",
        query_text="Where did she go to set up the pottery class",
    )
    await ms._bm25_precursor(object(), q)  # type: ignore[arg-type]
    sent = [*seen["tokens"], *seen["probes"], *seen["drop_if_unseen"]]
    assert "pottery" in seen["tokens"]
    assert not {"go", "to", "up"} & set(sent)
    assert all(len(t.encode()) >= bm25.MIN_TERM_LEN for t in sent)


# ---- fail-open reads say they failed ------------------------------------------


class _BoomCH:
    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        raise TimeoutError("read timed out")

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict | None:
        raise TimeoutError("read timed out")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_memory_cache")
async def test_memory_stats_failure_is_logged_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.memory.app.search_bm25 import _fetch_bm25_corpus_stats

    counted: list[dict[str, str]] = []

    class _Counter:
        def add(self, n: int, attributes: dict[str, str]) -> None:
            counted.append(attributes)

    monkeypatch.setattr(bm25, "_STATS_FAILURES", _Counter())
    with caplog.at_level(logging.WARNING, logger="server.core.bm25"):
        out = await _fetch_bm25_corpus_stats(_BoomCH(), org="o", namespace="n", tokens=["x"])
    assert out == ([], 0.0)  # still fails open
    assert counted == [{"plane": "memory", "error": "TimeoutError"}]
    assert any("TimeoutError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_knowledge_stats_failure_is_counted_and_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import bm25 as knowledge_bm25

    knowledge_bm25.reset_cache()
    counted: list[dict[str, str]] = []

    class _Counter:
        def add(self, n: int, attributes: dict[str, str]) -> None:
            counted.append(attributes)

    monkeypatch.setattr(bm25, "_STATS_FAILURES", _Counter())
    out = await knowledge_bm25.lexical_stats(
        _BoomCH(), org="o", namespaces=["n"], embedding_model="m", tokens=["k8s"]
    )
    assert out == (0.0, [])
    assert counted == [{"plane": "knowledge", "error": "TimeoutError"}]


# ---- knowledge corpus in words -------------------------------------------------


class _RecCH:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict | None:
        self.sqls.append(" ".join(sql.split()))
        return {"n": 3, "avgdl": 12.0}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.sqls.append(" ".join(sql.split()))
        if "GROUP BY namespace" in sql:  # the corpus statistics
            return [{"namespace": "n", "n": 3, "avgdl": 12.0}]
        return [{"term": "k8s", "df": 1}]


@pytest.mark.asyncio
async def test_knowledge_avgdl_is_the_mean_word_count() -> None:
    from server.knowledge.app import bm25 as knowledge_bm25

    knowledge_bm25.reset_cache()
    ch = _RecCH()
    avgdl, idfs = await knowledge_bm25.lexical_stats(
        ch, org="o", namespaces=["n"], embedding_model="m", tokens=["k8s", "pod"]
    )
    knowledge_bm25.reset_cache()
    assert avgdl == 12.0 and idfs == [bm25.idf(1, 3), bm25.idf(0, 3)]
    # a chunk's length counts the words of its context and its text
    assert (
        "avg(length(splitByNonAlpha(context_lc)) + length(splitByNonAlpha(content_lc)))"
        in ch.sqls[0]
    )
    assert "length(content)" not in ch.sqls[0] and "PREWHERE" not in ch.sqls[0]


def test_knowledge_df_views_and_rebuild_read_the_same_two_columns() -> None:
    """Document frequency comes from two views (a chunk's text and its context) and from
    `loom lexical-stats-rebuild`; both read the context and the text the lexical leg
    counts tf and length over (`test_knowledge_lexical`), or df and tf disagree on what
    a chunk contains."""
    import pathlib
    import re

    from server.memory.store.term_freq import _REBUILD_SQL

    text = (pathlib.Path(__file__).resolve().parents[2] / "schemas" / "schema.sql").read_text()
    views = re.findall(
        r"CREATE MATERIALIZED VIEW IF NOT EXISTS loom\.\w+ TO loom\.chunk_term_freq\b(.*?);",
        text,
        re.S,
    )
    read = {c for v in views for c in re.findall(r"splitByNonAlpha\((\w+)\)", v)}
    assert read == {"content", "context"}
    assert bm25.terms_sql("concat(context, ' ', content)") in _REBUILD_SQL["knowledge"][1]
