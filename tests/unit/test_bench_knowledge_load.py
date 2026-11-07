"""bench/knowledge_load.py: the declared service levels it checks against, and how it
scores a run. No server: the load itself runs against one (its docstring)."""

from __future__ import annotations

import pytest

from bench import knowledge_load as load
from server.settings.catalog import OP_BUDGETS_P99_MS

HEAD = "\n## Service levels\n\n| Measure | p95 ms | p99 ms |\n| --- | --- | --- |\n"


def test_each_declared_op_level_is_its_latency_budget() -> None:
    """An op's p99 level is its budget, so the load and the server's own
    budget.violated count against one number."""
    levels = load.service_levels(load.DOC.read_text())
    ops = {m for m in levels if m.startswith("knowledge.")}
    assert ops == {"knowledge.search", "knowledge.query", "knowledge.ingest"}
    for op in ops:
        p95, p99 = levels[op]
        assert p99 == OP_BUDGETS_P99_MS[op], op
        assert 0 < p95 <= p99, op
    assert load.INGEST_SMALL in levels
    # Every measured op the run reports is one the table can name.
    assert ops <= set(load.OPS)


@pytest.mark.parametrize(
    ("rows", "why"),
    [
        ("| `knowledge.search` | 150 |\n", "not a"),
        ("| `knowledge.search` | 150 | 0.25 |\n", "not a"),
        ("| `knowledge.search` | 150 | 250 |\n| `knowledge.search` | 100 | 250 |\n", "repeated"),
        ("", "no rows"),
    ],
)
def test_a_levels_table_it_cannot_read_whole_is_an_error(rows: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        load.service_levels(HEAD + rows)


def test_a_level_is_read_with_its_thousands_and_up_to_the_next_heading() -> None:
    page = HEAD + "| `knowledge.query` | 1,000 | 1,500 |\n\n### Notes\n\n| a | b |\n"
    assert load.service_levels(page) == {"knowledge.query": (1000.0, 1500.0)}


def test_a_measure_past_either_level_or_never_measured_is_reported() -> None:
    def measured(p95: float, p99: float, ok: int = 10) -> dict[str, object]:
        return {"ok": ok, "p95_ms": p95, "p99_ms": p99}

    levels = {"a": (100.0, 200.0), "b": (100.0, 200.0), "c": (100.0, 200.0), "d": (1.0, 2.0)}
    report = {"a": measured(90, 190), "b": measured(120, 190), "c": measured(90, 210)}
    past = load.verdict(report, levels)  # type: ignore[arg-type]
    assert [p.split(":")[0] for p in past] == ["b", "c", "d"]
    assert past[2] == "d: nothing measured"
    assert load.verdict({"a": measured(90, 190, ok=0)}, {"a": (100.0, 200.0)}) == [  # type: ignore[dict-item]
        "a: nothing measured"
    ]


def test_a_tally_times_successes_and_counts_rejected_calls_apart() -> None:
    t = load.Tally()
    for ms in (10.0, 20.0, 30.0, 40.0):
        t.add(ms, 200)
    t.add(5.0, 201)  # an upload's answer
    for status in (429, 503, 503, 500, 0):
        t.add(1.0, status)
    s = t.summary()
    assert (s["ok"], s["p50_ms"], s["p99_ms"]) == (5, 20.0, 40.0)
    assert s["errors"] == {"429": 1, "503": 2, "500": 1, "0": 1} and s["rejected"] == 3


def test_a_measure_with_failed_calls_is_past_however_fast_the_rest() -> None:
    fast = {"ok": 10, "p95_ms": 1.0, "p99_ms": 1.0}
    levels = {"a": (100.0, 200.0)}
    assert load.verdict({"a": {**fast, "errors": {"429": 50}}}, levels) == []
    (past,) = load.verdict({"a": {**fast, "errors": {"500": 90, "429": 3}}}, levels)
    assert past == "a: 90 calls failed {'500': 90}"


def test_the_queue_depth_is_summed_by_sample_slot_over_the_tenants_that_read_one() -> None:
    series = [[(0, 2), (1, 3), (1, 4)], [(0, 5), (2, 1)], []]
    assert load.combined_depth(series) == {0: 7, 1: 4, 2: 1}


def test_an_answer_that_is_not_json_is_a_failed_call_tallied_not_a_crash() -> None:
    import asyncio

    import httpx

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("knowledge.search"):
            return httpx.Response(502, text="<html>bad gateway</html>")
        return httpx.Response(200, text="not json")

    async def go() -> list[tuple[int, dict[str, object]]]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(answer), base_url="http://x"
        ) as c:
            run = load.Run(c, "o", "ns", 0.0, load._fresh_tallies(), load.Counter())
            out = [await run.op("knowledge.search", {}), await run.op("knowledge.query", {})]
            assert run.tallies["knowledge.search"].errors == {"502": 1}
            assert run.tallies["knowledge.query"].latencies_ms  # the 200 was timed
            return out

    assert asyncio.run(go()) == [(502, {}), (0, {})]


def test_calls_start_at_their_rate_whatever_the_earlier_ones_take() -> None:
    import asyncio
    import time

    async def go() -> list[float]:
        started: list[float] = []

        async def slow(n: int) -> None:
            started.append(time.monotonic())
            await asyncio.sleep(0.5)  # far longer than the 0.05 s between starts

        run = load.Run(None, "o", "ns", time.monotonic() + 0.3, {}, load.Counter())  # type: ignore[arg-type]
        t0 = time.monotonic()
        await run.paced(20.0, slow)
        return [t - t0 for t in started]

    starts = asyncio.run(go())
    assert len(starts) >= 5  # open loop: a slow call does not hold back the next
    assert all(b - a < 0.2 for a, b in zip(starts, starts[1:], strict=False))
