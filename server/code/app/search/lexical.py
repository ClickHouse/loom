"""The lexical leg: corpus statistics for the idfs, and the chunk scan."""

from __future__ import annotations

import logging
import time
from typing import Any

from server.code.app.search.state import _POOL
from server.code.app.spans import code_span, span_attrs
from server.code.domain.index import query_trim
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo
from server.core import bm25

logger = logging.getLogger(__name__)


async def _lexical_leg(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    run_lexical: bool,
    tokens: list[str],
    trim: query_trim.TrimPlan,
    work: dict[str, int],
    path_prefix: str,
    scope: SearchScope | None,
) -> tuple[list[dict[str, Any]], list[str], list[float]]:
    """The lexical leg: corpus stats for the idfs, then the chunk scan.
    `scope` is the constraint's predicate, None when unconstrained."""
    rows: list[dict[str, Any]] = []
    if not run_lexical:
        return rows, tokens, []
    # Fail soft, as the vector leg does: a corpus-stats or scan failure
    # (a replica refusing the read, a timeout) degrades the search to the
    # other leg instead of failing the whole call. The counter says so.
    try:
        lex_tokens, idfs = await _lexical_terms(
            repo,
            org,
            ns,
            tokens,
            work,
            trim,
        )
        if tokens:
            work["lexical_queries"] += 1
            with code_span("search.lexical_leg") as sp:
                t0 = time.perf_counter()
                rows = await repo.lexical_search_chunks(
                    org,
                    ns,
                    model,
                    lex_tokens,
                    idfs,
                    path_prefix=path_prefix,
                    pool=_POOL,
                    scope=scope,
                )
                # Leg wall time in ms so a bench can read the leg's cost off
                # the response instead of a trace (int: retrieval_work's type).
                work["lexical_leg_ms"] = int((time.perf_counter() - t0) * 1000)
                span_attrs(sp, results=len(rows))
    except Exception as e:  # noqa: BLE001 — vector-only degrade
        logger.warning("code.search lexical leg failed: %s", e)
        work["lexical_leg_failed"] = 1
        return [], tokens, []
    return rows, lex_tokens, idfs


async def _lexical_terms(
    repo: CodeRepo,
    org: str,
    ns: str,
    tokens: list[str],
    work: dict[str, int],
    trim: query_trim.TrimPlan | None = None,
) -> tuple[list[str], list[float]]:
    """The lexical leg's (tokens, idfs): one corpus-stats read.

    Under an active trim plan the stats read probes EVERY token of the cleaned question
    and the leg keeps the rarest `trim.cap` of them (identifiers always)."""
    from server.code.app.index import corpus_stats

    trimming = trim is not None and trim.active
    base = trim.all_tokens if trimming and trim is not None else tokens
    # The statistics the process already holds are not re-read
    df, n_chunks, _avgdl = await corpus_stats.stats(repo, org, ns, base, work)
    lex_tokens = tokens
    if trimming and trim is not None:
        lex_tokens = query_trim.select_rarest(base, df, trim.cap, trim.keep)
        work["query_trimmed_tokens_used"] = len(lex_tokens)
    idfs = [bm25.idf(df.get(t, 0), max(n_chunks, 1)) for t in lex_tokens]
    return lex_tokens, idfs
