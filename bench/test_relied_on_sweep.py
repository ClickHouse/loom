"""CH-free tests for bench/relied_on_sweep.py's pure core."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.relied_on_sweep import (
    ArmScores,
    cluster_keys,
    compare,
    ips_recall,
    per_question,
)
from bench.retrieval_recall import Row, score_one


def _resp(keys: list[str]) -> dict:
    return {"results": [{"memory_key": k, "distance": 0.2, "source": "fact"} for k in keys]}


def _rows(ranked: list[list[str]], gold: list[str]) -> list[Row]:
    return [score_one(_resp(r), gold) for r in ranked]


def test_per_question_reads_rank_of_first_gold_key() -> None:
    rows = _rows([["a", "g"], ["g"], ["x", "y", "z"]], ["g"])
    rr, hit = per_question(rows, k=2)
    assert rr == [0.5, 1.0, 0.0] and hit == [1.0, 1.0, 0.0]


def test_compare_reports_a_paired_bound_that_moves_with_the_arm() -> None:
    gold = ["g"]
    base = ArmScores({}, _rows([["x", "g"]] * 30, gold))  # rr 0.5 everywhere
    better = ArmScores({"w": 1}, _rows([["g", "x"]] * 30, gold))  # rr 1.0 everywhere
    same = ArmScores({"w": 2}, _rows([["x", "g"]] * 30, gold))
    res = compare(base, better, k=1, clusters=None, resamples=500)
    assert res["mrr"].mean == 0.5 and res["mrr"].lb > 0 and res["hit@1"].wins == 30
    res2 = compare(base, same, k=1, clusters=None, resamples=500)
    assert res2["mrr"].mean == 0.0 and res2["mrr"].lb <= 0 <= res2["mrr"].ub


def test_compare_refuses_misaligned_arms() -> None:
    import pytest

    with pytest.raises(ValueError):
        compare(
            ArmScores({}, _rows([["g"]], ["g"])),
            ArmScores({}, _rows([["g"], ["g"]], ["g"])),
            k=1,
            clusters=None,
        )


def test_cluster_keys_prefer_a_cluster_field_then_the_trace_agent() -> None:
    qs = [
        {"cluster": "c1"},
        {"note": "relied-on 1 of 5 returned; agent=ask; ts=2026"},
        {"note": "agent=-"},
    ]
    assert cluster_keys(qs) == ["c1", "ask", "q2"]
    assert cluster_keys([{"note": "no agent here"}]) is None


def test_a_single_cluster_or_the_read_all_agent_is_no_cluster() -> None:
    # every question from agent "*" → None, so the bootstrap resamples questions
    assert cluster_keys([{"note": "agent=*"}] * 5) is None
    # one real cluster covering everything → None (a zero-width interval otherwise)
    assert cluster_keys([{"cluster": "only"}] * 5) is None


def test_ips_recall_weights_a_gold_key_by_the_inverse_of_its_shown_rank_propensity() -> None:
    prop = {"1": 0.6, "14": 0.3}
    gold = ["top", "deep"]
    shown = {"top": 1, "deep": 14}

    # the arm returns only the key the labeller showed at rank 1: weight 1/0.6 of 1/0.6 + 1/0.3
    def ips(keys: list[str]) -> float:
        return ips_recall(score_one(_resp(keys), gold), k=10, shown_ranks=shown, propensity=prop)

    only_top = ips(["top", "x"])
    assert round(only_top, 3) == round((1 / 0.6) / (1 / 0.6 + 1 / 0.3), 3)
    # returning the deep key instead is worth twice as much
    assert ips(["deep", "x"]) > only_top
    # both → 1.0; neither → 0.0; beyond k does not count
    assert ips(["deep", "top"]) == 1.0
    assert ips(["x"] * 10 + ["top"]) == 0.0
    # no propensity curve → plain gold recall
    plain = ips_recall(score_one(_resp(["top", "x"]), gold), k=10, shown_ranks=shown, propensity={})
    assert plain == 0.5


def test_compare_adds_the_ips_metric_only_when_the_dataset_carries_shown_ranks() -> None:
    gold = ["g", "h"]
    base = ArmScores({}, _rows([["g", "x"]] * 10, gold))
    other = ArmScores({"m": 1}, _rows([["h", "x"]] * 10, gold))
    qs = [{"gold_shown_ranks": {"g": 1, "h": 12}}] * 10
    prop = {"1": 0.6, "12": 0.3}
    res = compare(base, other, k=5, clusters=None, resamples=300, questions=qs, propensity=prop)
    assert "ips_recall@5" in res and res["ips_recall@5"].mean > 0  # surfacing the deep key wins
    assert "ips_recall@5" not in compare(base, other, k=5, clusters=None, resamples=300)
