"""memory.search's latency attribution reaches the event ledger:
`retrieval_work.stage_ms` / `loop_lag_ms` flatten to five `out_*` columns
(the whole partition), bounded and never raising."""

from __future__ import annotations

from server.observability.event_args import _retrieval_work_columns, project_result


def test_stage_and_lag_flatten_to_four_columns() -> None:
    work = {
        "loop_lag_ms": 12.345,
        "stage_ms": {"scope": 21.4, "fusion": 143.6, "vector_legs": 137.7, "total": 333.1},
        "inline_rerank_used": False,
    }
    cols = _retrieval_work_columns(work)
    assert cols == {
        "out_loop_lag_ms": "12.35",
        "out_stage_total_ms": "333.1",
        "out_stage_top": "fusion",
        "out_stage_top_ms": "143.6",
        # The whole partition, largest first, whole ms.
        "out_stage_ms": "fusion=144,vector_legs=138,scope=21",
    }


def test_the_whole_partition_is_bounded_with_the_largest_stages_surviving() -> None:
    from server.observability.event_args import _MAX_IDENT_CHARS

    stages = {f"stage_{i:02d}_with_a_long_name": float(i) for i in range(40)}
    stages["total"] = 1000.0
    cols = _retrieval_work_columns({"stage_ms": stages})
    packed = cols["out_stage_ms"]
    assert len(packed) <= _MAX_IDENT_CHARS
    assert packed.startswith("stage_39_with_a_long_name=39,stage_38_with_a_long_name=38")
    assert "total=" not in packed


def test_project_result_carries_them_beside_the_identifier_columns() -> None:
    result = {
        "search_mode": "semantic",
        "retrieval_work": {"loop_lag_ms": 0.5, "stage_ms": {"embed": 1.0, "total": 2.0}},
        "results": [{"memory_key": "k"}],
    }
    out = project_result(result)
    assert out["out_search_mode"] == "semantic"
    assert out["out_stage_top"] == "embed" and out["out_stage_total_ms"] == "2.0"
    assert out["out_loop_lag_ms"] == "0.50"
    assert "out_retrieval_work" not in out


def test_odd_shapes_yield_nothing() -> None:
    assert _retrieval_work_columns(None) == {}
    assert _retrieval_work_columns({}) == {}
    assert _retrieval_work_columns({"stage_ms": "nope", "loop_lag_ms": True}) == {}
    assert _retrieval_work_columns({"stage_ms": {"total": "x"}}) == {}
    assert project_result({"retrieval_work": 3}) == {}


def test_project_result_says_whether_a_read_answered() -> None:
    """A get that misses and a bulk_get with missing keys are both
    200 / success=1; the ledger carries the outcome as counts so a found
    rate per operation is one aggregate — the keys themselves stay off the row."""
    from server.observability.event_args import project_result

    assert project_result({"found": False, "memory_key": "k"})["out_found"] == "false"
    assert project_result({"found": True, "memory_key": "k"})["out_found"] == "true"
    bulk = project_result({"results": {"a": {}, "b": {}}, "missing": ["chunk:memory.set:x:0"]})
    assert (bulk["out_found_n"], bulk["out_missing_n"]) == ("2", "1")
    assert "out_missing" not in bulk  # counts, never the key list
    # anything else keeps its shape: no found column invented
    assert "out_found" not in project_result({"memory_key": "k"})
    assert "out_found" not in project_result({"found": "yes"})
