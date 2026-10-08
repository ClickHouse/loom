"""The graph expansion walks `hops` hops from every seed.

CH-free: the expansion against a stub client that records which seeds each
hop was asked about.
"""

from __future__ import annotations

from typing import Any

import pytest


class _CH:
    def __init__(self, hops: list[list[dict[str, Any]]]) -> None:
        self.hops = hops
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        if "FROM loom.entity_degree" in sql:
            return []
        return self.hops.pop(0) if self.hops else []


def _hop_count(ch: _CH) -> int:
    return sum(1 for s, _ in ch.calls if "FROM loom.entity_edges" in s)


@pytest.mark.asyncio
async def test_one_hop_by_default_and_hops_walks_further() -> None:
    from server.memory.app.retrieval import fetch_graph_neighbors

    hop1 = [{"neighbor": "acme", "weight": 3, "via_seed": "alice"}]
    hop2 = [{"neighbor": "x", "weight": 1, "via_seed": "acme"}]
    ch = _CH([list(hop1), list(hop2)])
    out = await fetch_graph_neighbors(
        ch, org="o", namespace="n", seed_entities=["alice"], hub_skip_degree=0
    )
    assert out == ["acme"] and _hop_count(ch) == 1
    ch = _CH([list(hop1), list(hop2)])
    paths: dict[str, list[str]] = {}
    out = await fetch_graph_neighbors(
        ch,
        org="o",
        namespace="n",
        seed_entities=["alice"],
        hub_skip_degree=0,
        hops=2,
        paths=paths,
    )
    assert out == ["acme", "x"] and _hop_count(ch) == 2
    hop_queries = [p for s, p in ch.calls if "FROM loom.entity_edges" in s]
    assert hop_queries[1]["seeds"] == ["acme"]
    assert paths["x"] == ["alice", "acme", "x"]
