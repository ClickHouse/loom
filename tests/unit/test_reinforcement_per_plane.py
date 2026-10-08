"""The reinforcement fold ranks within the pool, pool-centred: the pool's net
lift is zero, and a chunk row joins the fold through its parent fact's key."""

from __future__ import annotations

from datetime import UTC, datetime

from server.memory.app.reinforcement import apply_folded_reinforcement

NOW = datetime(2026, 9, 16, tzinfo=UTC)
W = 0.15


def _fact(key: str, score: float, base: int = 1) -> dict:
    return {
        "agent": "a",
        "user_id": "-",
        "memory_key": key,
        "reinforcement_base": base,
        "created_at": NOW,
        "rank_score": score,
        "reinforcement_sql": 0.0,
    }


def _chunk(key: str, parent: str, score: float) -> dict:
    return {
        "plane": "chunk",
        "memory_key": key,
        "parent_memory_key": parent,
        "rank_score": score,
        "reinforcement_sql": 0.0,
        "created_at": NOW,
    }


def _folded(**counts: int) -> dict:
    return {("a", "-", k): (n, NOW) for k, n in counts.items()}


def _apply(rows: list[dict], folded: dict) -> int:
    return apply_folded_reinforcement(rows, folded, weight=W, half_life_days=90.0, now=NOW)


def test_net_lift_is_zero_and_the_hottest_row_leads() -> None:
    rows = [_fact("hot", 0.5), _fact("warm", 0.5), _fact("cold", 0.5)]
    _apply(rows, _folded(hot=100, warm=10, cold=1))
    assert abs(sum(r["reinforcement_sql"] for r in rows)) < 1e-9
    assert rows[0]["rank_score"] > rows[1]["rank_score"] > rows[2]["rank_score"]
    assert rows[0]["reinforcement_sql"] <= W and rows[2]["reinforcement_sql"] >= -W


def test_uniform_single_or_cold_pool_gets_nothing() -> None:
    uniform = [_fact("a", 0.5), _fact("b", 0.5)]
    assert _apply(uniform, _folded(a=7, b=7)) == 0
    single = [_fact("only", 0.5)]
    assert _apply(single, _folded(only=40)) == 0
    cold = [_fact("a", 0.5), _fact("b", 0.4)]
    assert _apply(cold, {}) == 0
    assert [r["rank_score"] for r in cold] == [0.5, 0.4]


def test_the_sql_absolute_term_is_replaced() -> None:
    # A folded row can arrive with the SQL's capped boost inside rank_score.
    hot = _fact("hot", 0.60 + 0.5, base=1000)
    hot["reinforcement_sql"] = 0.5
    cold = _fact("cold", 0.65)
    rows = [hot, cold]
    _apply(rows, {})
    # shares 1 and 0, mean 0.5: hot +W/2, cold -W/2
    assert abs(hot["reinforcement_sql"] - W / 2) < 1e-12
    assert abs(hot["rank_score"] - (0.60 + W / 2)) < 1e-12
    assert abs(cold["rank_score"] - (0.65 - W / 2)) < 1e-12


def test_a_chunk_inherits_its_parent() -> None:
    rows = [_fact("f1", 0.70), _chunk("c1", "f1", 0.60), _chunk("c2", "orphan", 0.60)]
    _apply(rows, _folded(f1=50))
    # Parent and chunk share the pool maximum; the orphan sits at the pool's zero.
    assert abs(rows[0]["reinforcement_sql"] - rows[1]["reinforcement_sql"]) < 1e-12
    assert rows[0]["reinforcement_sql"] > 0 > rows[2]["reinforcement_sql"]


def test_chunk_lookup_takes_the_largest_count_over_agents() -> None:
    rows = [_fact("p", 0.5), _chunk("c1", "p", 0.5)]
    folded = {("a", "-", "p"): (3, NOW), ("b", "u", "p"): (30, NOW)}
    _apply(rows, folded)
    # fact row ("a","-","p") = 3, chunk via by-key = 30 → chunk holds the max
    assert rows[1]["reinforcement_sql"] > rows[0]["reinforcement_sql"]
