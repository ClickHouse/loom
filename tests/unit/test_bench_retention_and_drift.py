"""bench/retention_curve.py and bench/entity_drift.py — the pure parts."""

from __future__ import annotations

from bench import entity_drift as ed
from bench import retention_curve as rc


def test_facts_and_noise_are_deterministic_distinct_and_keyed() -> None:
    a, b = rc.make_facts(5, seed=1), rc.make_facts(5, seed=1)
    assert a == b and len({f["key"] for f in a}) == 5 and all("question" in f for f in a)
    assert rc.make_facts(5, seed=2) != a
    n1, n2 = rc.make_noise(3, period=1), rc.make_noise(3, period=2)
    assert {x["key"] for x in n1}.isdisjoint({x["key"] for x in n2})
    assert "recipes" not in n2[0]["value"] or "recipes" not in n1[0]["value"]  # topics rotate


def test_score_period_and_render() -> None:
    ranks = {"a": 1, "b": 7, "c": 12, "d": None}
    p = rc.score_period(3, rows=200, ranks=ranks, cites=[(1.0, True), (-1.0, False)])
    assert p.recall_at_5 == 0.25 and p.recall_at_10 == 0.5 and p.mean_rank == round(20 / 3, 2)
    assert p.lost == 2 and p.cite_score_mean == 0.0 and p.gold_cited_share == 0.5
    p0 = rc.score_period(0, rows=20, ranks={"a": 1, "b": 2, "c": 3, "d": 4})
    text = rc.render([p0, p])
    assert "| 0 | 20 | 1.000 | 1.000 |" in text and "recall@10 1.000 → 0.500 (-0.500)" in text
    assert rc.gold_rank(["x", "a"], "a") == 2 and rc.gold_rank(["x"], "a") is None


def test_drift_series_orders_by_version_and_measures_steps() -> None:
    rows = [
        {
            "namespace": "ns",
            "entity": "Marta",
            "version": 2,
            "summary_embedding": [0.0, 1.0],
            "fact_count": 3,
        },
        {
            "namespace": "ns",
            "entity": "Marta",
            "version": 1,
            "summary_embedding": [1.0, 0.0],
            "fact_count": 2,
        },
        {
            "namespace": "ns",
            "entity": "Marta",
            "version": 3,
            "summary_embedding": [0.0, 1.0],
            "fact_count": 4,
        },
        {
            "namespace": "ns",
            "entity": "Kenji",
            "version": 1,
            "summary_embedding": [1.0, 0.0],
            "fact_count": 1,
        },
        {
            "namespace": "ns",
            "entity": "Kenji",
            "version": 2,
            "summary_embedding": [1.0, 0.0],
            "fact_count": 1,
        },
    ]
    drifts = ed.drift_series(rows)
    assert [d.entity for d in drifts] == ["Marta", "Kenji"]
    m = drifts[0]
    assert (
        m.versions == 3
        and m.max_step == 1.0
        and m.last_step == 0.0
        and m.total_path == 1.0
        and m.fact_count_last == 4
    )
    assert drifts[1].max_step == 0.0
    assert (
        ed.cosine_distance([1.0, 0.0], [0.0, 1.0]) == 1.0 and ed.cosine_distance([], [1.0]) is None
    )
    s = ed.summarise(drifts)
    assert s["entities_with_history"] == 2 and s["max_step_over_0_1"] == 1
    assert "| ns | Marta | 3 | 1.000 |" in ed.render(drifts, top=5)
