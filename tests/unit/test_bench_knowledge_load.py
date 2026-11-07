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
