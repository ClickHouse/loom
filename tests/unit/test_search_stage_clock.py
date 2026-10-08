"""Memory.search charges wall time to named stages and samples
event-loop lag, and bench/search_latency.py summarises the payload per band."""

from __future__ import annotations

import asyncio

from server.memory.app.search.clock import StageClock, sample_loop_lag_ms


def test_marks_are_cumulative_and_total_shares_the_origin() -> None:
    work: dict = {}
    clock = StageClock(work)
    clock.mark("plan")
    clock.mark("embed")
    clock.mark("plan")  # a stage that runs twice adds up
    total = clock.finish()
    stages = work["stage_ms"]
    assert set(stages) == {"plan", "embed", "total"}
    assert all(v >= 0.0 for v in stages.values())
    assert stages["total"] == round(total, 1)
    # The stages partition the total (up to rounding of each part).
    assert abs((stages["plan"] + stages["embed"]) - stages["total"]) <= 0.3


def test_loop_lag_sample_is_small_on_an_idle_loop() -> None:
    lag = asyncio.run(sample_loop_lag_ms())
    assert 0.0 <= lag < 50.0


def test_payload_carries_stage_ms_and_loop_lag() -> None:
    from types import SimpleNamespace

    from server.memory.app.search.telemetry import _actual_work_payload

    work: dict = {"vector_queries": 1}
    clock = StageClock(work)
    clock.mark("embed")
    budget = SimpleNamespace(
        llm_calls_used=0,
        vector_queries_used=1,
        max_llm_calls=2,
        max_vector_queries=12,
        llm_calls_metered=0,
    )
    out = _actual_work_payload(work, budget, clock=clock)  # type: ignore[arg-type]
    assert "total" in out["stage_ms"] and "embed" in out["stage_ms"]


def test_bench_summary_groups_by_band_and_orders_stages() -> None:
    from bench.search_latency import band_for, summarize

    assert band_for(999) == "<1k" and band_for(5_000) == "1k-10k"
    assert band_for(50_000) == "10k-100k" and band_for(500_000) == "100k+"
    assert band_for(None) == "unknown"
    rows = [
        {
            "band": "<1k",
            "namespace": "a",
            "status": 200,
            "wall_ms": 100.0,
            "loop_lag_ms": 0.1,
            "stage_ms": {"embed": 60.0, "vector_legs": 30.0, "total": 95.0},
        },
        {
            "band": "<1k",
            "namespace": "b",
            "status": 200,
            "wall_ms": 300.0,
            "loop_lag_ms": 0.3,
            "stage_ms": {"embed": 80.0, "vector_legs": 200.0, "total": 290.0},
        },
        {"band": "<1k", "namespace": "b", "status": 500, "wall_ms": 5.0, "stage_ms": {}},
        {
            "band": "10k-100k",
            "namespace": "c",
            "status": 200,
            "wall_ms": 900.0,
            "loop_lag_ms": 12.0,
            "stage_ms": {"embed": 70.0, "rerank": 700.0, "total": 880.0},
        },
    ]
    summary = summarize(rows)
    assert [s["band"] for s in summary] == ["10k-100k", "<1k"]
    small = next(s for s in summary if s["band"] == "<1k")
    assert small["calls"] == 2 and small["namespaces"] == 2  # the 500 is excluded
    assert small["mean_server_total_ms"] == 192.5
    stage_cols = [k for k in small if k.startswith("stage_")]
    assert stage_cols[0] == "stage_vector_legs_ms"  # largest stage first
    assert small["stage_embed_ms"] == 70.0
    big = next(s for s in summary if s["band"] == "10k-100k")
    assert big["mean_loop_lag_ms"] == 12.0 and big["stage_rerank_ms"] == 700.0
