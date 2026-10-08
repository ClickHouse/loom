"""PageRank over the file-level include/call graph: which files the rest of the
repository leans on most.

Pure numpy power iteration on a CSR-ish adjacency built from edge tuples —
no networkx (an extra dep for one algorithm), no scipy. 20–40M edges fits:
the matrix is never materialized, only (src_idx, dst_idx) arrays.

numpy is imported lazily; the weekly worker runs this in asyncio.to_thread.
"""

from __future__ import annotations


def pagerank(
    edges: list[tuple[str, str]],
    *,
    damping: float = 0.85,
    iterations: int = 30,
) -> dict[str, float]:
    """node -> rank for a directed edge list. Deterministic, dangling-safe."""
    import numpy as np

    nodes: dict[str, int] = {}
    for a, b in edges:
        for node in (a, b):
            if node not in nodes:
                nodes[node] = len(nodes)
    n = len(nodes)
    if n == 0:
        return {}

    src = np.fromiter((nodes[a] for a, _ in edges), dtype=np.int64, count=len(edges))
    dst = np.fromiter((nodes[b] for _, b in edges), dtype=np.int64, count=len(edges))

    out_deg = np.bincount(src, minlength=n).astype(np.float64)
    rank = np.full(n, 1.0 / n, dtype=np.float64)

    for _ in range(iterations):
        contrib = np.where(out_deg[src] > 0, rank[src] / out_deg[src], 0.0)
        new_rank = np.zeros(n, dtype=np.float64)
        np.add.at(new_rank, dst, contrib)
        # dangling mass + teleport
        dangling = rank[out_deg == 0].sum()
        new_rank = (1 - damping) / n + damping * (new_rank + dangling / n)
        rank = new_rank

    inv = {i: name for name, i in nodes.items()}
    return {inv[i]: float(rank[i]) for i in range(n)}


def degrees(edges: list[tuple[str, str]]) -> dict[str, tuple[int, int]]:
    """node -> (in_degree, out_degree)."""
    ind: dict[str, int] = {}
    outd: dict[str, int] = {}
    for a, b in edges:
        outd[a] = outd.get(a, 0) + 1
        ind[b] = ind.get(b, 0) + 1
    return {n: (ind.get(n, 0), outd.get(n, 0)) for n in set(ind) | set(outd)}
