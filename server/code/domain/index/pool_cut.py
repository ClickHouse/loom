"""The lexical leg's pool cut on the path penalties of the ranking. Pure.

The ranking gives a row of the leg `factor / (RRF_K + rank)`: its reciprocal-rank vote
times the factor of its path (`path_class`), 1 on source. A leg that returns its first
`pool` rows by rank drops source rows whose vote counts for more than the vote of the
test and prose rows it keeps. The cut keeps the `pool` rows with the largest vote, and
each votes with the rank it has in the whole leg, so a row in the pool scores what it
would score had the leg returned every row.
"""

from __future__ import annotations

import math

# Reciprocal-rank fusion's constant: a row at rank r of a leg votes 1 / (RRF_K + r).
RRF_K = 60.0

# ClickHouse fetches the body columns of an `ORDER BY … LIMIT n` lazily only for n at
# or under query_plan_max_limit_for_lazy_materialization (10,000 by default).
MAX_DEPTH = 10_000


def cut_depth(pool: int, noise_penalty: float, prose_penalty: float) -> int:
    """How many rows of the leg's own order can hold a row of the cut pool, for the two
    path factors of the ranking.

    A path gets 1, one of the factors or both: `lo` and `hi` are the smallest and the
    largest of those. A row at rank r votes at most hi / (RRF_K + r), and each of the
    first `pool` rows votes at least lo / (RRF_K + pool). Past rank
    (RRF_K + pool) * hi / lo - RRF_K a row loses to all of them, so it is not in the
    pool. The depth stops at MAX_DEPTH: with no factor over 1, a factor under about
    0.026 reads that deep and no deeper."""
    factors = (1.0, noise_penalty, prose_penalty, noise_penalty * prose_penalty)
    lo, hi = min(factors), max(factors)
    if lo <= 0.0:
        return MAX_DEPTH
    return min(MAX_DEPTH, max(pool, math.ceil((RRF_K + pool) * hi / lo - RRF_K)))
