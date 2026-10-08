"""Okapi BM25, stated once for every lexical plane.

The memory, knowledge and code planes score their lexical legs in ClickHouse
SQL built here; the tracker's title-weighted BM25F shares the constants and the
IDF. Corpus statistics stay with each plane (their tables differ); what they
feed in is the same: per-token document frequency, the corpus size and the
average document length, in the unit the plane's tf counts.

    idf(t)   = ln(1 + (N - df + 0.5) / (df + 0.5))
    score(d) = sum_t idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
"""

from __future__ import annotations

import logging
import math
import string
from typing import Any

log = logging.getLogger(__name__)

K1 = 1.2
B = 0.75

# The shortest term, in bytes as ClickHouse's `length` counts them, that the
# memory and knowledge term-frequency views index. A shorter query token has
# no document frequency there, so it would take the maximum IDF while its
# substring tf matches nearly every row: the query side drops it too. The
# views' SQL is built from this constant (`terms_sql`); the schema's CREATE
# statements spell the same literal, and a unit test pins the two together.
MIN_TERM_LEN = 3

# What ClickHouse's splitByNonAlpha splits on: ASCII whitespace and ASCII
# punctuation except `~`. Everything else, digits and non-ASCII bytes
# included, stays inside a token.
_SEPARATORS = frozenset(string.whitespace + string.punctuation.replace("~", ""))


def idf(df: int, n: int) -> float:
    """+0.5-smoothed IDF; positive for every term, an unseen one included."""
    return math.log(1.0 + (max(n, 0) - df + 0.5) / (df + 0.5))


def term(
    tf: float, idf_t: float, *, dl: float, avgdl: float, k1: float = K1, b: float = B
) -> float:
    """One term's weight, as `bm25_sum_sql` computes it; the denominator is
    floored at 1 so an unmatched term is 0, never 0/0."""
    return idf_t * tf * (k1 + 1.0) / max(tf + k1 * (1.0 - b + b * dl / avgdl), 1.0)


def query_tokens(text: str, *, min_len: int) -> list[str]:
    """The query's terms as the term-frequency views write them:
    `splitByNonAlpha` over `lowerUTF8`, kept when `length(t) >= min_len`
    (bytes, as ClickHouse counts), de-duplicated in order (IDFs are aligned
    by position). Digits stay in the token: `s3bucket`, `k8s`, `ipv6`."""
    out: list[str] = []
    seen: set[str] = set()
    buf: list[str] = []

    def flush() -> None:
        if buf:
            tok = "".join(buf)
            if len(tok.encode()) >= min_len and tok not in seen:
                seen.add(tok)
                out.append(tok)
            buf.clear()

    for ch in (text or "").lower():
        if ch in _SEPARATORS:
            flush()
        else:
            buf.append(ch)
    flush()
    return out


def floor_tokens(tokens: list[str]) -> list[str]:
    """The tokens the term-frequency views can know: those of at least
    `MIN_TERM_LEN` bytes, in order."""
    return [t for t in tokens if len(t.encode()) >= MIN_TERM_LEN]


def terms_sql(column: str) -> str:
    """The distinct terms of `column` as the term-frequency views write them:
    `splitByNonAlpha` over `lowerUTF8`, at least `MIN_TERM_LEN` bytes."""
    return (
        f"arrayDistinct(arrayFilter(t -> length(t) >= {MIN_TERM_LEN}, "
        f"arrayMap(t -> lowerUTF8(t), splitByNonAlpha({column}))))"
    )


def tf_array_sql(tf: str, tokens: str) -> str:
    """Each query token's term frequency in the row, computed once per token:
    `tf` is an expression over the lambda variable `tok`."""
    return f"arrayMap(tok -> {tf}, {tokens})"


def bm25_sum_sql(
    tfs: str,
    idfs: str,
    *,
    dl: str,
    avgdl: str,
    k1: str | float = K1,
    b: str | float = B,
) -> str:
    """The BM25 sum over aligned `tfs` / `idfs` arrays. `dl` and `avgdl` are
    SQL in the same unit; `k1` / `b` are literals or bound parameters. The
    lambda names `tf` and `idf` must not be columns of the scanned table."""
    return (
        f"arraySum(arrayMap((tf, idf) -> idf * tf * ({k1} + 1) / greatest("
        f"tf + {k1} * (1 - {b} + {b} * {dl} / {avgdl}), 1.0), {tfs}, {idfs}))"
    )


_STATS_FAILURES: Any = None


def record_stats_failure(plane: str, exc: BaseException) -> None:
    """A corpus-statistics read failed and the plane scores without BM25:
    one warning and one `loom.search.bm25_stats_failures` count, by plane and
    exception class. Never raises."""
    global _STATS_FAILURES
    log.warning(
        "%s BM25 corpus stats read failed, lexical leg unweighted: %s", plane, type(exc).__name__
    )
    try:
        from opentelemetry import metrics as _metrics

        if _STATS_FAILURES is None:
            _STATS_FAILURES = _metrics.get_meter("loom.search").create_counter(
                name="loom.search.bm25_stats_failures",
                description="BM25 corpus-statistics reads that failed open, by plane and error",
                unit="1",
            )
        _STATS_FAILURES.add(1, attributes={"plane": plane[:32], "error": type(exc).__name__[:64]})
    except Exception:  # noqa: BLE001 — telemetry must never fail the search
        pass


__all__ = [
    "B",
    "K1",
    "MIN_TERM_LEN",
    "bm25_sum_sql",
    "floor_tokens",
    "idf",
    "query_tokens",
    "record_stats_failure",
    "term",
    "terms_sql",
    "tf_array_sql",
]
