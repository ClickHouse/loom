"""code.search keeps the corpus statistics its BM25 leg needs.

Every lexical leg opens with the document frequency of each query token and the
namespace's chunk count and mean length. `CodeRepo.code_corpus_stats` reads them from
`code_term_df`, an hourly snapshot of plain numbers, in one statement; a namespace the
snapshot does not hold yet is read from `code_term_freq` (a `uniqExactMerge` over each
token's aggregate states, a granule of object storage per token on a cold cache) and
`code_doc_stats`. The numbers change only as the index changes and matter only through
idf and avgdl.

This module keeps them in the process for `settings.code_corpus_stats_ttl_s`
seconds: the namespace block (N, avgdl) per (org, namespace) and the df per
(org, namespace, term), a term the corpus does not know cached as 0 so it
is not asked for again. A search reads only the terms it has not seen; a
search whose terms are all known opens the lexical leg with no statistics
read at all, and says so in `retrieval_work.corpus_stats_cached`. Bounded
(`code_corpus_stats_max_terms`, oldest term out); TTL 0 bypasses the cache.
Per process: each web worker warms its own.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from server.settings import settings

hits = 0
misses = 0

# (org, namespace) -> (expires_at, n_chunks, avgdl)
_NS: dict[tuple[str, str], tuple[float, int, float]] = {}
# (org, namespace, term) -> (expires_at, df); insertion order = recency
_DF: OrderedDict[tuple[str, str, str], tuple[float, int]] = OrderedDict()


def _ttl_s() -> float:
    return float(settings.code_corpus_stats_ttl_s)


def _cap() -> int:
    return int(settings.code_corpus_stats_max_terms)


async def stats(
    repo: Any,
    org: str,
    ns: str,
    tokens: list[str],
    work: dict[str, int] | None = None,
    *,
    now: float | None = None,
) -> tuple[dict[str, int], int, float]:
    """`(df per known token, N chunks, avgdl)` with the shape of
    `CodeRepo.code_corpus_stats`: only tokens the corpus knows appear in the
    dict. Reads the repo for the tokens the cache lacks (and the namespace
    block with them, the way the repo's read already pairs them); a
    namespace block alone re-reads one token. A repo failure propagates
    and the lexical leg degrades."""
    global hits, misses
    ttl = _ttl_s()
    if ttl <= 0 or not tokens:
        direct: tuple[dict[str, int], int, float] = await repo.code_corpus_stats(org, ns, tokens)
        return direct
    t = time.monotonic() if now is None else now
    df: dict[str, int] = {}
    missing: list[str] = []
    for term in dict.fromkeys(tokens):
        key = (org, ns, term)
        hit = _DF.get(key)
        if hit is not None and hit[0] > t:
            _DF.move_to_end(key)
            if hit[1] > 0:
                df[term] = hit[1]
        else:
            missing.append(term)
    block = _NS.get((org, ns))
    if not missing and block is not None and block[0] > t:
        hits += 1
        if work is not None:
            work["corpus_stats_cached"] = 1
        return df, block[1], block[2]
    misses += 1
    probe = missing or [tokens[0]]
    got, n_chunks, avgdl = await repo.code_corpus_stats(org, ns, probe)
    if work is not None:
        work["corpus_stats_terms_read"] = len(probe)
    n_chunks, avgdl = int(n_chunks), float(avgdl)
    for term in probe:
        v = int(got.get(term, 0))
        if v > 0:
            df[term] = v
        elif term in df:
            del df[term]
    # a namespace with nothing indexed yet is not a fact worth keeping: its
    # first pass changes N every second
    if n_chunks > 0:
        expires = t + ttl
        for term in probe:
            key = (org, ns, term)
            _DF[key] = (expires, int(got.get(term, 0)))
            _DF.move_to_end(key)
        _NS[(org, ns)] = (expires, n_chunks, avgdl)
        cap = _cap()
        while cap > 0 and len(_DF) > cap:
            _DF.popitem(last=False)
    return df, n_chunks, avgdl


def size() -> tuple[int, int]:
    """(namespace blocks, terms) held — for tests and `loom doctor`."""
    return len(_NS), len(_DF)


def reset_for_tests() -> None:
    global hits, misses
    hits = misses = 0
    _NS.clear()
    _DF.clear()
