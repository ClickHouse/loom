"""The read path of the BM25 corpus-stats lookup. df must read the uniqExact
aggregate state and N must come from count() over the deduped doc_stats
ReplacingMergeTree FINAL; a SummingMergeTree sum(df)/sum(total_docs) shape
would silently return 0 against these columns.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from server.memory.app.search_bm25 import _fetch_bm25_corpus_stats


@pytest.fixture(autouse=True)
def _fresh_bm25_reader_state():
    """The reader caches a namespace's corpus figure per process; it may not
    leak into another test's stub."""
    from server.memory.app import search_bm25 as m

    m._BM25_CORPUS_CACHE.clear()
    yield
    m._BM25_CORPUS_CACHE.clear()


class _StubCH:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.sqls.append(sql)
        if "uniqExactMerge" in sql:  # df-per-term query
            return [{"term": "fox", "df": 2}, {"term": "quick", "df": 2}]
        return [{"n": 10, "avgdl": 20.0}]  # corpus query


@pytest.mark.asyncio
async def test_bm25_read_path_uses_deduped_stat_shape() -> None:
    ch = _StubCH()
    idfs, avgdl = await _fetch_bm25_corpus_stats(
        ch, org="o", namespace="ns-bm25-unit-uniqexact", tokens=["fox", "quick", "unseen"]
    )
    joined = " ".join(ch.sqls)
    # df reads the uniqExact aggregate state, not a per-insert sum.
    assert "uniqExactMerge(df_state)" in joined
    assert "sum(df)" not in joined
    # N/avgdl come from count() over the deduped doc_stats under FINAL.
    assert "count()" in joined
    assert "FROM loom.memory_doc_stats FINAL" in joined
    assert "total_docs" not in joined and "total_excerpt_length" not in joined
    # idf math + token alignment unchanged.
    assert len(idfs) == 3 and avgdl == 20.0
    assert idfs[0] == pytest.approx(math.log(1.0 + (10 - 2 + 0.5) / (2 + 0.5)))
    # an unseen token has df=0 and a (larger) finite idf.
    assert idfs[2] == pytest.approx(math.log(1.0 + (10 - 0 + 0.5) / (0 + 0.5)))
    assert idfs[2] > idfs[0]


@pytest.mark.asyncio
async def test_bm25_reader_merges_the_live_tail() -> None:
    """By default both statements merge the roll-ups with the
    source rows newer than the roll-ups' sentinel mark (set-union states, so a
    document folded twice counts once)."""
    from server.memory.app import search_bm25 as m

    m._BM25_CORPUS_CACHE.clear()
    ch = _StubCH()
    idfs, avgdl = await _fetch_bm25_corpus_stats(ch, org="o", namespace="n", tokens=["fox"])
    assert len(idfs) == 1 and avgdl == 20.0
    joined = "\n".join(ch.sqls)
    assert joined.count("version > (SELECT max(ts_hwm) FROM loom.memory_term_freq") == 1
    assert joined.count("version > (SELECT max(ts_hwm) FROM loom.memory_doc_stats") == 1
    assert "WHERE org = '' AND namespace = ''" in joined  # the sentinel key, a PK lookup
    assert "uniqExactMerge(df_state)" in joined and "UNION ALL" in joined
    assert "argMax(len, v)" in joined  # a re-versioned doc counts once, at its newest length
    assert "PREWHERE" not in joined


@pytest.mark.asyncio
async def test_bm25_reader_setting_off_reads_the_rollups_alone(monkeypatch) -> None:
    from server.memory.app import search_bm25 as m
    from server.settings import settings

    m._BM25_CORPUS_CACHE.clear()
    monkeypatch.setattr(settings, "memory_search_bm25_live_tail", False)
    ch = _StubCH()
    await _fetch_bm25_corpus_stats(ch, org="o", namespace="n", tokens=["fox"])
    assert not any("ts_hwm" in q for q in ch.sqls)
    assert "FROM loom.memory_doc_stats FINAL" in "\n".join(ch.sqls)
