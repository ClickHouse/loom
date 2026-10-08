"""Paired, cluster-aware bootstrap for A/B deltas: the number a ranking
change ships on.

A point estimate on 60 questions moves by one question. What a gate needs is
the LOWER BOUND of the paired delta: resample the questions (or the clusters
they belong to; questions about the same mechanism are not independent draws),
recompute the mean delta each time, and read the 2.5th percentile. A gain ships
only when that bound clears zero.

Assumptions: the percentile interval of a nonparametric bootstrap, with whole
clusters resampled with replacement. It is honest when the clusters are many
and exchangeable; with a handful of clusters the interval is too narrow, so
read `clusters` beside the bound. The point estimate is the item mean, which is
what the resampled means average to.

Pure Python, deterministic under `seed`, no numpy.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BootResult:
    n: int
    clusters: int
    mean: float
    lb: float
    ub: float
    wins: int
    ties: int
    losses: int
    resamples: int

    @property
    def significant_gain(self) -> bool:
        return self.lb > 0

    def line(self, unit: str = "", scale: float = 1.0, digits: int = 1) -> str:
        f = f"{{:+.{digits}f}}"
        lo, hi = f.format(self.lb * scale), f.format(self.ub * scale)
        wtl = f"{self.wins}/{self.ties}/{self.losses}"
        return (
            f"paired delta {f.format(self.mean * scale)}{unit} "
            f"(95% bootstrap {lo}{unit} .. {hi}{unit}; "
            f"n={self.n}, clusters={self.clusters}, win/tie/loss {wtl})"
        )


def paired_bootstrap(
    deltas: Sequence[float],
    clusters: Sequence[str] | None = None,
    *,
    resamples: int = 10_000,
    seed: int = 7,
    alpha: float = 0.05,
    tie_eps: float = 1e-9,
) -> BootResult:
    """Bootstrap the mean of paired per-item deltas (B minus A).

    With `clusters`, whole clusters are resampled with replacement and every
    item in a drawn cluster contributes — the clustered bootstrap that keeps
    correlated questions from narrowing the interval. Without clusters each
    item is its own cluster."""
    n = len(deltas)
    if n == 0:
        return BootResult(0, 0, 0.0, 0.0, 0.0, 0, 0, 0, 0)
    keys = list(clusters) if clusters is not None else [str(i) for i in range(n)]
    if len(keys) != n:
        raise ValueError("clusters must align with deltas")
    groups: dict[str, list[float]] = defaultdict(list)
    for k, d in zip(keys, deltas, strict=True):
        groups[k].append(float(d))
    members = list(groups.values())
    mean = sum(deltas) / n
    wins = sum(1 for d in deltas if d > tie_eps)
    losses = sum(1 for d in deltas if d < -tie_eps)
    ties = n - wins - losses
    rng = random.Random(seed)
    means: list[float] = []
    m = len(members)
    for _ in range(max(1, resamples)):
        total = 0.0
        count = 0
        for _j in range(m):
            g = members[rng.randrange(m)]
            total += sum(g)
            count += len(g)
        means.append(total / count if count else 0.0)
    means.sort()
    lo_i = max(0, min(len(means) - 1, int(round((alpha / 2) * (len(means) - 1)))))
    hi_i = max(0, min(len(means) - 1, int(round((1 - alpha / 2) * (len(means) - 1)))))
    return BootResult(
        n=n,
        clusters=m,
        mean=mean,
        lb=means[lo_i],
        ub=means[hi_i],
        wins=wins,
        ties=ties,
        losses=losses,
        resamples=len(means),
    )


def cluster_of_path(path: str, depth: int = 2) -> str:
    """Cluster key for a gold path: its first `depth` directory segments
    (`src/Storages/MergeTree/x.cpp` -> `src/Storages`). Questions about the
    same subsystem move together under a ranking change."""
    parts = [p for p in (path or "").strip("/").split("/") if p]
    return "/".join(parts[:depth]) if parts else "(none)"
