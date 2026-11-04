"""BM25 corpus statistics for knowledge.search.

The knowledge plane's lexical leg mirrors memory's: per-token inverse document
frequency from the term-frequency materialized views (`loom.chunk_term_freq`,
uniqExact over the chunk key, fed from each chunk's text and its context),
corpus size and average chunk length in words (context and text) from
`loom.chunks` (cached per namespace and summed over the namespaces a search
reads), Okapi BM25 (`server.core.bm25`) scored in SQL over the query tokens,
fused with the vector leg by reciprocal rank. Tokenisation here must match the
views: lower-cased `splitByNonAlpha` runs, digits included, of at least
`bm25.MIN_TERM_LEN` bytes.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Protocol

from server.core import bm25
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
from server.settings import settings


class _StatsClient(Protocol):
    async def query_one(
        self, sql: str, params: dict[str, Any] | None = ...
    ) -> dict[str, Any] | None: ...

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = ...
    ) -> list[dict[str, Any]]: ...


MIN_TOKEN_LEN = bm25.MIN_TERM_LEN
_CORPUS_CACHE: OrderedDict[tuple[str, str, str], tuple[float, int, float]] = OrderedDict()
# One entry per namespace a search read; the oldest go past this many.
_CORPUS_CACHE_MAX = 50_000  # not a setting: an in-process cache's memory bound


def tokenize(text: str) -> list[str]:
    """Query-side tokens as `loom.chunk_term_freq` writes them: the shared
    BM25 tokenizer at this plane's minimum length."""
    return bm25.query_tokens(text, min_len=MIN_TOKEN_LEN)


async def corpus_stats(
    ch: _StatsClient, *, org: str, namespaces: list[str], embedding_model: str
) -> tuple[int, float]:
    """(N, avgdl) over the live chunks of `namespaces`, each namespace's pair
    cached for `knowledge_bm25_corpus_ttl_s` and the ones missing read in one statement. avgdl is
    in words of the same split the lexical leg counts tf and dl in."""
    now = time.time()
    stats: dict[str, tuple[int, float]] = {}
    missing: list[str] = []
    for ns in namespaces:
        cached = _CORPUS_CACHE.get((org, ns, embedding_model))
        if cached and now - cached[0] < settings.knowledge_bm25_corpus_ttl_s:
            stats[ns] = (cached[1], cached[2])
        else:
            missing.append(ns)
    if missing:
        rows = await ch.query_all(
            """
            SELECT namespace, count() AS n,
                   toFloat32(avg(length(splitByNonAlpha(context_lc))
                                 + length(splitByNonAlpha(content_lc)))) AS avgdl
            FROM loom.chunks
            WHERE org = {org:String} AND namespace IN {nss:Array(String)}
              AND embedding_model = {model:String}
              AND """
            + LIVE_CHUNK_SQL
            + """ AND content != ''
            GROUP BY namespace
            """,
            params={"org": org, "nss": missing, "model": embedding_model},
        )
        found = {str(r["namespace"]): (int(r["n"] or 0), float(r["avgdl"] or 0.0)) for r in rows}
        for ns in missing:
            stats[ns] = found.get(ns, (0, 0.0))
            _CORPUS_CACHE[(org, ns, embedding_model)] = (now, *stats[ns])
            _CORPUS_CACHE.move_to_end((org, ns, embedding_model))
        while len(_CORPUS_CACHE) > _CORPUS_CACHE_MAX:
            _CORPUS_CACHE.popitem(last=False)
    n = sum(count for count, _ in stats.values())
    words = sum(count * avgdl for count, avgdl in stats.values())
    return n, (words / n if n else 0.0)


async def idfs_for(
    ch: _StatsClient, *, org: str, namespaces: list[str], tokens: list[str], n_docs: int
) -> list[float]:
    """Per-token IDF aligned with `tokens`, its document frequency summed over
    `namespaces`; a token absent from the view has df 0 and gets the maximum
    IDF (rare term, if it matches at all)."""
    if not tokens:
        return []
    rows = await ch.query_all(
        """
        SELECT term, sum(df) AS df
        FROM (
            SELECT term, namespace, uniqExactMerge(df_state) AS df
            FROM loom.chunk_term_freq
            WHERE org = {org:String} AND namespace IN {nss:Array(String)}
              AND term IN {terms:Array(String)}
            GROUP BY term, namespace
        )
        GROUP BY term
        """,
        params={"org": org, "nss": namespaces, "terms": tokens},
    )
    df_by_term: dict[str, int] = {str(r["term"]): int(r["df"] or 0) for r in rows}
    return [bm25.idf(df_by_term.get(t, 0), n_docs) for t in tokens]


async def lexical_stats(
    ch: _StatsClient,
    *,
    org: str,
    namespaces: list[str],
    embedding_model: str,
    tokens: list[str],
) -> tuple[float, list[float]]:
    """(avgdl, idfs aligned with `tokens`) for the lexical leg over
    `namespaces`; (0.0, []) on a cold corpus or a failed read, which leaves the
    vector leg (and, in hybrid mode, the exact-key leg)."""
    try:
        n_docs, avgdl = await corpus_stats(
            ch, org=org, namespaces=namespaces, embedding_model=embedding_model
        )
        if n_docs <= 0:
            return avgdl, []
        return avgdl, await idfs_for(
            ch, org=org, namespaces=namespaces, tokens=tokens, n_docs=n_docs
        )
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
