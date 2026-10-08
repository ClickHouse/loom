"""BM25 corpus statistics for knowledge.search.

The knowledge plane's lexical leg mirrors memory's: per-token inverse
document frequency from the term-frequency materialized view
(`loom.chunk_term_freq`, uniqExact over the chunk key), corpus size and
average chunk length in words from `loom.chunks` (cached per namespace), Okapi
BM25 (`server.core.bm25`) scored in SQL over the query tokens, fused with the
vector leg by reciprocal rank. Tokenisation here must match the view: lower-cased
`splitByNonAlpha` runs, digits included, of at least `bm25.MIN_TERM_LEN` bytes.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from server.core import bm25
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL


class _StatsClient(Protocol):
    async def query_one(
        self, sql: str, params: dict[str, Any] | None = ...
    ) -> dict[str, Any] | None: ...

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = ...
    ) -> list[dict[str, Any]]: ...


MIN_TOKEN_LEN = bm25.MIN_TERM_LEN
_CORPUS_CACHE: dict[tuple[str, str, str], tuple[float, int, float]] = {}
_CORPUS_CACHE_TTL_S = 300.0


def tokenize(text: str) -> list[str]:
    """Query-side tokens as `loom.chunk_term_freq` writes them: the shared
    BM25 tokenizer at this plane's minimum length."""
    return bm25.query_tokens(text, min_len=MIN_TOKEN_LEN)


async def corpus_stats(
    ch: _StatsClient, *, org: str, namespace: str, embedding_model: str
) -> tuple[int, float]:
    """(N, avgdl) over the namespace's live chunks, cached for 5 minutes.
    avgdl is in words of the same split the lexical leg counts tf and dl in."""
    key = (org, namespace, embedding_model)
    cached = _CORPUS_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _CORPUS_CACHE_TTL_S:
        return cached[1], cached[2]
    row = await ch.query_one(
        """
        SELECT count() AS n, toFloat32(avg(length(splitByNonAlpha(content_lc)))) AS avgdl
        FROM loom.chunks
        WHERE org = {org:String} AND namespace = {ns:String}
          AND embedding_model = {model:String}
          AND """
        + LIVE_CHUNK_SQL
        + """ AND content != ''
        """,
        params={"org": org, "ns": namespace, "model": embedding_model},
    )
    n = int((row or {}).get("n") or 0)
    avgdl = float((row or {}).get("avgdl") or 0.0)
    _CORPUS_CACHE[key] = (now, n, avgdl)
    return n, avgdl


async def idfs_for(
    ch: _StatsClient, *, org: str, namespace: str, tokens: list[str], n_docs: int
) -> list[float]:
    """Per-token IDF aligned with `tokens`; a token absent from the view has
    df 0 and gets the maximum IDF (rare term, if it matches at all)."""
    if not tokens:
        return []
    rows = await ch.query_all(
        """
        SELECT term, uniqExactMerge(df_state) AS df
        FROM loom.chunk_term_freq
        WHERE org = {org:String} AND namespace = {ns:String}
          AND term IN {terms:Array(String)}
        GROUP BY term
        """,
        params={"org": org, "ns": namespace, "terms": tokens},
    )
    df_by_term: dict[str, int] = {str(r["term"]): int(r["df"] or 0) for r in rows}
    return [bm25.idf(df_by_term.get(t, 0), n_docs) for t in tokens]


async def lexical_stats(
    ch: _StatsClient, *, org: str, namespace: str, embedding_model: str, tokens: list[str]
) -> tuple[float, list[float]]:
    """(avgdl, idfs aligned with `tokens`) for the lexical leg; (0.0, []) on a
    cold namespace or a failed read, which leaves the vector leg alone."""
    try:
        n_docs, avgdl = await corpus_stats(
            ch, org=org, namespace=namespace, embedding_model=embedding_model
        )
        if n_docs <= 0:
            return avgdl, []
        return avgdl, await idfs_for(ch, org=org, namespace=namespace, tokens=tokens, n_docs=n_docs)
    except Exception as e:  # noqa: BLE001 — stats are an optimisation of ranking
        bm25.record_stats_failure("knowledge", e)
        return 0.0, []


def reset_cache() -> None:
    _CORPUS_CACHE.clear()


__all__: list[str] = [
    "MIN_TOKEN_LEN",
    "tokenize",
    "corpus_stats",
    "idfs_for",
    "lexical_stats",
    "reset_cache",
]
