"""Pure-function tests for the scale harness — no server, no store, no spend."""

from __future__ import annotations

import httpx

from bench.scale_load import _error_code, _outcomes, _pct, _summary, tenant_sql


def test_the_single_shape_puts_every_key_in_one_namespace() -> None:
    assert tenant_sql("single", 10, 2) == ("'scale-one'", "'big'")


def test_the_zipf_shape_names_orgs_and_namespaces_by_tenant_rank() -> None:
    org, ns = tenant_sql("zipf", 10000, 1000)
    # 10 namespaces per org; the rank is T^u - 1 for u in [0, 1), so below T
    assert "intDiv(" in org and ", 10))" in org and "% 10" in ns
    assert "pow(10000, (n * 0.6180339887498949) % 1)" in org


def test_a_percentile_is_nearest_rank() -> None:
    assert _pct([], 0.5) == 0.0
    assert _pct([5.0, 1.0, 3.0], 0.5) == 3.0
    assert _pct([float(i) for i in range(1, 101)], 0.99) == 99.0
    assert _summary([1.0, 2.0], {"503:X": 1}) == {
        "n": 2,
        "p50_ms": 1.0,
        "p99_ms": 2.0,
        "mean_ms": 1.5,
        "errors": {"503:X": 1},
    }


def test_a_writes_outcome_is_read_from_its_answer() -> None:
    assert _outcomes("/v1/memory.set", {"noop": True}) == ["noop"]
    assert _outcomes("/v1/memory.set", {"duplicate_of": "m1"}) == ["duplicate"]
    assert _outcomes("/v1/memory.set", {"status": "active"}) == ["written"]
    bulk = {"written": [{}, {}], "duplicates": [{}], "merged": [], "unchecked": 2}
    assert sorted(_outcomes("/v1/memory.bulk_set", bulk)) == [
        "duplicate",
        "unchecked",
        "unchecked",
        "written",
        "written",
    ]
    assert _outcomes("/v1/memory.search", {"results": [{}, {}]}) == ["hits", "hits"]


def test_a_refusals_code_is_its_loom_error_code() -> None:
    body = {"detail": {"code": "ADMISSION_CONTENDED", "message": "busy"}}
    assert _error_code(httpx.Response(503, json=body)) == "ADMISSION_CONTENDED"
    assert _error_code(httpx.Response(502, text="bad gateway")) == ""
    assert _error_code(httpx.Response(500, json={"error": "x"})) == ""
