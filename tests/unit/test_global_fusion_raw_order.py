"""The global fusion pass keeps each plane's raw rank_score (planes are not
scaled by their best similarity, which loses recall at every k): with no
additive boost, the pool is ordered by raw score whatever plane a row came
from."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_global_fusion_orders_by_raw_score_across_planes(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.search import ranking as hs

    def _wire(key: str, source: str, distance: float, raw: float) -> SimpleNamespace:
        return SimpleNamespace(
            memory_key=key,
            namespace="ns",
            source=source,
            distance=distance,
            rank_score=raw,
            score_components={"rank_score_raw": raw},
            entities=[],
            content_excerpt="x",
        )

    monkeypatch.setattr(hs, "_effective_ann_boosts", lambda hits, rrf_pool: {})
    monkeypatch.setattr(hs, "_effective_arm_boosts", lambda hits, **kw: {})
    monkeypatch.setattr(
        "server.memory.domain.query_planner.identifier_tokens", lambda q: [], raising=False
    )
    hits = [_wire("f", "fact", 0.2, 0.60), _wire("c", "chunk", 0.7, 0.62)]
    inp = SimpleNamespace(search_mode="semantic", query="q", namespace="ns")
    ctx = SimpleNamespace(all_hits=hits, input=inp, actual_work={}, resolved_dates=[])
    hs._apply_global_fusion(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["c", "f"]  # chunk's raw 0.62 wins untouched
    assert "plane_gates" not in ctx.actual_work
    assert all("plane_gate" not in h.score_components for h in ctx.all_hits)
    assert [h.rank_score for h in ctx.all_hits] == [0.62, 0.60]
