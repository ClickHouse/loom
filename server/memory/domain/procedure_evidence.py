"""Evidence arithmetic for the procedure plane — pure functions, no I/O.

- **Precondition and goal are two factors, not one string.** Retrieving on the
  goal alone can score below no memory at all, because a matching goal with the
  wrong starting state injects the wrong trajectory. The two-axis score is the
  geometric mean of the situation (precondition) cosine and the goal cosine, so
  a strong match on one axis cannot rescue a miss on the other, while the scale
  stays that of a single cosine so status tiers, tool boosts and the ledger
  utility compose unchanged.

- **Failure is read at its lower confidence bound.** A Beta-Binomial
  posterior on a failure rate, read at mean − sd, is the failure a step or procedure has
  *established*, not the failure it might have (T = μ − σ). The fail-prone flag and the
  strike rule read this estimator, so a single unlucky run on a thin count flags nothing.
"""

from __future__ import annotations

import math


def failure_posterior(total: int, ok: int) -> tuple[float, float]:
    """Beta(fail+1, ok+1) posterior on the failure rate: (mean, sd).

    Laplace prior: with no runs the mean is 0.5 and the sd is its widest.
    """
    total = max(0, int(total))
    ok = min(total, max(0, int(ok)))
    fail = total - ok
    mean = (fail + 1.0) / (total + 2.0)
    var = mean * (1.0 - mean) / (total + 3.0)
    return mean, math.sqrt(max(0.0, var))


def failure_lcb(total: int, ok: int) -> float:
    """Lower confidence bound on the failure rate, mean − sd, floored at 0."""
    mean, sd = failure_posterior(total, ok)
    return max(0.0, mean - sd)


def failure_lcb_counts(runs: float, fails: float) -> float:
    """`failure_lcb` over fractional counts — edge traversals are weights.
    Beta(fails+1, runs−fails+1) at mean − sd, floored at 0."""
    runs = max(0.0, float(runs))
    fails = min(runs, max(0.0, float(fails)))
    mean = (fails + 1.0) / (runs + 2.0)
    var = mean * (1.0 - mean) / (runs + 3.0)
    return max(0.0, mean - math.sqrt(max(0.0, var)))


def two_axis_score(situation_similarity: float, goal_similarity: float | None) -> float:
    """Geometric mean of the two cosines; the situation cosine alone when
    the goal axis is unavailable. Negative cosines clamp to 0 so a goal
    that points the other way zeroes the score rather than flipping it."""
    a = max(0.0, float(situation_similarity))
    if goal_similarity is None:
        return a
    b = max(0.0, float(goal_similarity))
    return math.sqrt(a * b)


def cosine(a: list[float], b: list[float]) -> float | None:
    """Cosine similarity, or None when the vectors cannot be compared
    (different length, empty, or zero norm)."""
    if not a or not b or len(a) != len(b):
        return None
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return None
    return dot / math.sqrt(na * nb)
