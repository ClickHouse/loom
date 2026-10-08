"""The reinforcement term ranks within the pool instead of saturating at the
SQL term's absolute cap."""

from __future__ import annotations

from datetime import datetime

from server.memory.app.reinforcement import apply_folded_reinforcement


def _rows():
    now = datetime(2026, 9, 15, 7)
    return [
        {
            "agent": "a",
            "user_id": "-",
            "memory_key": "generic",
            "reinforcement_base": 1000,
            "last_reinforced_base": now,
            "created_at": now,
            "rank_score": 0.60,
            "reinforcement_sql": 0.5,
        },
        {
            "agent": "a",
            "user_id": "-",
            "memory_key": "specific",
            "reinforcement_base": 30,
            "last_reinforced_base": now,
            "created_at": now,
            "rank_score": 0.65,
            "reinforcement_sql": 0.5,
        },
        {
            "agent": "a",
            "user_id": "-",
            "memory_key": "fresh",
            "reinforcement_base": 1,
            "last_reinforced_base": now,
            "created_at": now,
            "rank_score": 0.70,
            "reinforcement_sql": 0.0,
        },
    ], now


def test_ranks_within_the_pool_and_never_exceeds_the_weight() -> None:
    rows, now = _rows()
    folded = {("a", "-", "generic"): (1000, now), ("a", "-", "specific"): (30, now)}
    changed = apply_folded_reinforcement(rows, folded, weight=0.15, half_life_days=90, now=now)
    assert changed == 3
    g, s, f = (r["reinforcement_sql"] for r in rows)
    assert g > s > f and g <= 0.15 and abs(g + s + f) < 1e-9
    # rank_score moved by the difference against the SQL's absolute boost
    assert abs(rows[0]["rank_score"] - (0.60 - 0.5 + g)) < 1e-9
    assert abs(rows[1]["rank_score"] - (0.65 - 0.5 + s)) < 1e-9


def test_no_fold_rows_still_scales_the_sql_bases() -> None:
    rows, now = _rows()
    changed = apply_folded_reinforcement(rows, {}, weight=0.1, half_life_days=90, now=now)
    g, s, f = (r["reinforcement_sql"] for r in rows)
    assert changed == 3 and g > s > f and abs(g + s + f) < 1e-9
