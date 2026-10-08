"""bench/plane_precision.py — per-plane tally (network-free)."""

from __future__ import annotations

from bench.plane_precision import tally


def _h(source: str, session: int) -> dict:
    return {"source": source, "session_id": f"ns:session_{session}"}


def test_tally_counts_precision_and_removal_effect() -> None:
    q1 = ([_h("summary", 3), _h("chunk", 7), _h("fact", 7), _h("summary", 1)], {7})
    q2 = ([_h("summary", 2), _h("summary", 5), _h("chunk", 9)], {2})
    out = tally([q1, q2], 10)
    s, c, f = out["planes"]["summary"], out["planes"]["chunk"], out["planes"]["fact"]
    assert s["hits"] == 4 and s["from_gold_session"] == 1 and s["precision"] == 0.25
    assert c["hits"] == 2 and c["precision"] == 0.5 and f["precision"] == 1.0
    assert out["gold_session_hit_at_k"] == 2
    # without digests q2 loses its only gold hit; without chunks nothing is lost
    assert s["gold_session_hit_at_k_without_plane"] == 1
    assert c["gold_session_hit_at_k_without_plane"] == 2
    assert s["first_gold_rank_mean"] == 1.0 and f["first_gold_rank_mean"] == 3.0
