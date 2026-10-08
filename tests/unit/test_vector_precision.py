"""bench/vector_precision.py — pure parts of the bf16-vs-f32 top-k comparison."""

from __future__ import annotations

from bench.vector_precision import CAST_BF16, overlap_at_k, rank_of, summarize, topk_sql


def test_overlap_and_rank() -> None:
    assert overlap_at_k(["a", "b", "c"], ["c", "a", "x"], 3) == 2 / 3
    assert overlap_at_k(["a"], ["a"], 1) == 1.0 and overlap_at_k([], [], 0) == 0.0
    assert rank_of("b", ["a", "b"]) == 2 and rank_of("z", ["a"]) is None


def test_summary_counts_identical_and_missing() -> None:
    rows = [
        {"overlap": 1.0, "top1_same": True, "f32_top1_rank_under_bf16": 1},
        {"overlap": 0.9, "top1_same": True, "f32_top1_rank_under_bf16": 1},
        {"overlap": 0.7, "top1_same": False, "f32_top1_rank_under_bf16": None},
    ]
    s = summarize(rows, 10)
    assert s["queries"] == 3 and s["queries_with_identical_topk"] == 1
    assert s["top1_agreement"] == round(2 / 3, 4) and s["f32_top1_missing_under_bf16"] == 1
    assert s["overlap_at_k_min"] == 0.7 and s["f32_top1_rank_under_bf16_max"] == 1
    assert summarize([], 10) == {"queries": 0}


def test_sql_casts_only_where_asked_and_is_read_only() -> None:
    plain = topk_sql(cast_query=False, cast_rows=False, k=10)
    both = topk_sql(cast_query=True, cast_rows=True, k=10)
    assert "BFloat16" not in plain and both.count("BFloat16") == 2
    assert "memory_key != {self:String}" in plain and "LIMIT 10" in plain
    assert plain.lstrip().upper().startswith("SELECT")
    assert CAST_BF16.format(col="embedding").startswith("arrayMap(")
