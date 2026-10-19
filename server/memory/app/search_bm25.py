"""BM25 corpus-statistics helpers for memory search."""

from __future__ import annotations

from typing import Any

from server.core import bm25
from server.memory.domain.search_types import _CHClient
from server.settings import settings

_BM25_CORPUS_CACHE: dict[tuple[str, str], tuple[float, int, float]] = {}


def _bm25_sigmoid_params(n_terms: int, max_score: float) -> tuple[float, float]:
    """Query-length-adaptive (midpoint, steepness) for the BM25 sigmoid.

    A linear `raw / max_possible` divide compresses BM25 scores into a tiny
    band near 0, so the lexical leg barely moves RRF order. Instead squash
    through a logistic centered at a fraction of the achievable max, slope
    by term count: short queries want a sharp curve separating a strong hit
    from noise, long queries a gentler one so partial matches still register.

    Returns (midpoint, steepness) for
        norm = 1 / (1 + exp(-steepness * (raw - midpoint))).
    """
    n = max(1, int(n_terms))
    ceiling = max(float(max_score), 1e-6)
    # ~35% of the achievable max already reads as a strong lexical match.
    midpoint = 0.35 * ceiling
    # Normalize steepness by score scale so the curve shape is scale-free.
    if n <= 2:
        slope = 6.0
    elif n <= 5:
        slope = 4.0
    else:
        slope = 2.5
    steepness = slope / ceiling
    return midpoint, steepness


# Identity of a document in the BM25 corpus: the six-field key both roll-ups
# dedup on.
_BM25_DOC_IDENT = (
    "(toString(agent), user_id, session_id, toString(scope), toString(embedding_model), memory_key)"
)
_BM25_TERMS = bm25.terms_sql("content_excerpt")
_BM25_LIVE_ROWS = (
    "org = {org:String} AND namespace = {ns:String} AND embedding_dim > 0 AND tombstoned_at IS NULL"
)

# Roll-ups alone: what the insert-time views kept exact at the write, and what
# the refreshable views keep exact up to 70 s after it.
_DF_SQL_ROLLUP = """
SELECT term, toUInt64(uniqExactMerge(df_state)) AS df
FROM loom.memory_term_freq
WHERE org = {org:String} AND namespace = {ns:String}
  AND term IN {tokens:Array(String)}
GROUP BY term
"""
_CORPUS_SQL_ROLLUP = """
SELECT
    toUInt64(count()) AS n,
    toFloat32(sum(excerpt_length) / nullif(count(), 0)) AS avgdl
FROM loom.memory_doc_stats FINAL
WHERE org = {org:String} AND namespace = {ns:String}
"""

# Roll-ups plus the live tail: the roll-up's states UNION ALL
# a uniqExactState over the namespace's source rows newer than the roll-up's
# mark, merged — set union, so a document folded by both counts once and the
# figures are exact at any instant. The mark is the sentinel row's ts_hwm
# (org = '', namespace = ''), a primary-key lookup. The tail predicate is
# `version > mark` on the namespace's own granules: on the largest tenant it
# adds 0.5 MB and no wall time to either statement.
_DF_SQL_TAIL = f"""
SELECT term, toUInt64(uniqExactMerge(df_state)) AS df
FROM (
    SELECT term, df_state
    FROM loom.memory_term_freq
    WHERE org = {{org:String}} AND namespace = {{ns:String}}
      AND term IN {{tokens:Array(String)}}
    UNION ALL
    SELECT term, uniqExactState({_BM25_DOC_IDENT}) AS df_state
    FROM (
        SELECT agent, user_id, session_id, scope, embedding_model, memory_key, content_excerpt
        FROM loom.memory_embeddings
        WHERE {_BM25_LIVE_ROWS}
          AND version > (SELECT max(ts_hwm) FROM loom.memory_term_freq
                         WHERE org = '' AND namespace = '' AND term = '')
    )
    ARRAY JOIN {_BM25_TERMS} AS term
    WHERE term IN {{tokens:Array(String)}}
    GROUP BY term
)
GROUP BY term
"""
_CORPUS_SQL_TAIL = f"""
SELECT toUInt64(count()) AS n, toFloat32(avg(len)) AS avgdl
FROM (
    SELECT ident, argMax(len, v) AS len
    FROM (
        SELECT {_BM25_DOC_IDENT} AS ident, excerpt_length AS len, version AS v
        FROM loom.memory_doc_stats FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
        UNION ALL
        SELECT {_BM25_DOC_IDENT} AS ident, toUInt32(length(content_excerpt)) AS len, version AS v
        FROM loom.memory_embeddings
        WHERE {_BM25_LIVE_ROWS}
          AND version > (SELECT max(ts_hwm) FROM loom.memory_doc_stats
                         WHERE org = '' AND namespace = '')
    )
    GROUP BY ident
)
"""


def _live_tail_wanted() -> bool:
    from server.settings import settings

    return bool(settings.memory_search_bm25_live_tail)


def compound_query_tokens(
    words: list[str], *, max_words: int = 6, max_len: int = 48, cap: int = 40
) -> list[str]:
    """The concatenations of adjacent query words, longest
    first, as candidate lexical tokens. A store that writes identifiers as
    one token — `ClickPipesUserCertificateNotReady`, `dcache_change_impact`
    — indexes them as one lowercased term (the term-frequency roll-up and
    the text index both split on non-alpha), so a query that spells the
    same identifier in words never meets it token for token. The caller
    keeps only the candidates the roll-up knows (df > 0); this is pure.
    Words are the query's alphabetic runs in order, lowercased."""
    out: list[str] = []
    seen: set[str] = set()
    n = len(words)
    for width in range(min(max_words, n), 1, -1):
        for i in range(0, n - width + 1):
            tok = "".join(words[i : i + width])
            if len(tok) > max_len or len(tok.encode()) < bm25.MIN_TERM_LEN or tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
            if len(out) >= cap:
                return out
    return out


async def fetch_bm25_corpus_stats_probing(
    ch: _CHClient,
    *,
    org: str,
    namespace: str,
    tokens: list[str],
    probes: list[str],
    drop_if_unseen: frozenset[str] = frozenset(),
) -> tuple[list[str], list[float], float]:
    """`_fetch_bm25_corpus_stats` over `tokens` plus those of
    `probes` the roll-up knows (df > 0), in one df read. Returns the final
    token list (tokens, then the surviving probes in their given order),
    the aligned IDFs and avgdl. An unseen probe would take the unseen-token
    IDF and dilute every real match, so it is dropped, not scored; so is a
    token in `drop_if_unseen` (a lemma whose raw word is also a token)."""
    probes = [p for p in probes if p and p not in set(tokens)]
    if not probes and not drop_if_unseen:
        idfs, avgdl = await _fetch_bm25_corpus_stats(
            ch, org=org, namespace=namespace, tokens=tokens
        )
        return list(tokens), idfs, avgdl
    stats = await _bm25_df_and_corpus(ch, org=org, namespace=namespace, tokens=[*tokens, *probes])
    if stats is None:
        return list(tokens), [], 0.0
    df_by_term, n_total, avgdl = stats
    kept = [t for t in tokens if t not in drop_if_unseen or df_by_term.get(t, 0) > 0]
    final = [*kept, *[p for p in probes if df_by_term.get(p, 0) > 0]]
    return final, [bm25.idf(df_by_term.get(t, 0), n_total) for t in final], avgdl


async def _fetch_bm25_corpus_stats(
    ch: _CHClient,
    *,
    org: str,
    namespace: str,
    tokens: list[str],
) -> tuple[list[float], float]:
    """Look up per-token IDF + corpus avg-doc-length from the BM25 roll-ups
    (loom.memory_term_freq + loom.memory_doc_stats).

    df and N are deduped over the full document key via uniqExact (term_freq)
    and ReplacingMergeTree FINAL (doc_stats), so re-embeds / reinforcements
    don't inflate the stats. The roll-ups are appended
    once a minute; the default statements merge them with the live tail of
    source rows newer than the roll-ups' mark, so the figures
    are exact at any instant.

    Returns (idfs_aligned_with_tokens, avgdl). Empty roll-ups (cold namespace)
    return ([], 0.0) and the caller falls back to token-overlap scoring.

    idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5)).
    """
    if not tokens:
        return [], 0.0
    stats = await _bm25_df_and_corpus(ch, org=org, namespace=namespace, tokens=tokens)
    if stats is None:
        return [], 0.0
    df_by_term, n_total, avgdl = stats
    return [bm25.idf(df_by_term.get(t, 0), n_total) for t in tokens], avgdl


async def _bm25_df_and_corpus(
    ch: _CHClient, *, org: str, namespace: str, tokens: list[str]
) -> tuple[dict[str, int], int, float] | None:
    """The df-per-term and corpus (N, avgdl) reads behind the two public
    readers; None when the roll-ups are cold or the read failed."""
    import asyncio as _asyncio
    import time as _time

    cache_key = (org, namespace)
    cached = _BM25_CORPUS_CACHE.get(cache_key)
    use_cached_corpus = (
        cached is not None and _time.time() - cached[0] < settings.memory_search_bm25_cache_ttl_s
    )
    df_params = {"org": org, "ns": namespace, "tokens": tokens}
    corpus_params = {"org": org, "ns": namespace}

    async def _read(tail: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        df_sql = _DF_SQL_TAIL if tail else _DF_SQL_ROLLUP
        df_task = ch.query_all(df_sql, params=df_params)
        if use_cached_corpus:
            return await df_task, None
        corpus_sql = _CORPUS_SQL_TAIL if tail else _CORPUS_SQL_ROLLUP
        corpus_task = ch.query_all(corpus_sql, params=corpus_params)
        corpus_rows, df_rows = await _asyncio.gather(corpus_task, df_task)
        return df_rows, corpus_rows

    try:
        df_rows, corpus_rows = await _read(_live_tail_wanted())
        if use_cached_corpus:
            _, n_total, avgdl = cached  # type: ignore[misc]
        else:
            if not corpus_rows:
                return None
            n_total = int(corpus_rows[0].get("n") or 0)
            avgdl = float(corpus_rows[0].get("avgdl") or 0.0)
            if n_total <= 0 or avgdl <= 0:
                return None
            _BM25_CORPUS_CACHE[cache_key] = (_time.time(), n_total, avgdl)
    except Exception as e:  # noqa: BLE001 — a failed BM25 stats read disables BM25 scoring
        bm25.record_stats_failure("memory", e)
        return None

    df_by_term: dict[str, int] = {
        str(r["term"]): int(r.get("df") or 0) for r in df_rows if r.get("term") is not None
    }
    return df_by_term, n_total, avgdl
