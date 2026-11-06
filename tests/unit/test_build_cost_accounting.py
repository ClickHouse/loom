"""Build-side cost is a first-class number — the extraction
yield metric on the server and the efficiency columns in the bench."""

from __future__ import annotations

import contextvars
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class _Instr:
    def __init__(self) -> None:
        self.adds: list[tuple[int, dict[str, Any]]] = []
        self.records: list[tuple[float, dict[str, Any]]] = []

    def add(self, n: int, attributes: dict[str, Any]) -> None:
        self.adds.append((n, attributes))

    def record(self, v: float, attributes: dict[str, Any]) -> None:
        self.records.append((v, attributes))


def test_record_extraction_build_splits_direction_and_computes_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import metrics

    tokens, yld = _Instr(), _Instr()
    monkeypatch.setattr(metrics, "_memory_build_tokens", tokens)
    monkeypatch.setattr(metrics, "_memory_extraction_yield", yld)
    metrics.record_extraction_build(
        namespace="ns", tokens_in=15000, tokens_out=600, written=12, messages=40
    )
    assert [(n, a["direction"]) for n, a in tokens.adds] == [(15000, "in"), (600, "out")]
    assert all(a["stage"] == "extract" and a["loom.namespace"] == "ns" for _, a in tokens.adds)
    ((v, a),) = yld.records
    assert v == 15000 / 12 and a["messages"] == "21-100"
    # nothing written: the whole prompt is the cost of one (non-)memory
    yld.records.clear()
    metrics.record_extraction_build(namespace="ns", tokens_in=9000, tokens_out=10, written=0)
    assert yld.records[0][0] == 9000.0
    # uninitialised instruments: silent no-op
    monkeypatch.setattr(metrics, "_memory_build_tokens", None)
    monkeypatch.setattr(metrics, "_memory_extraction_yield", None)
    metrics.record_extraction_build(namespace="ns", tokens_in=1, tokens_out=1, written=1)


def test_handler_accounts_build_on_the_way_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.extract import handler as h
    from server.observability import metrics

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(metrics, "record_extraction_build", lambda **kw: seen.append(kw))
    h._account_build("ns", 1200, 80, 3, 9)
    assert seen == [
        {"namespace": "ns", "tokens_in": 1200, "tokens_out": 80, "written": 3, "messages": 9}
    ]

    def _boom(**kw: Any) -> None:
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(metrics, "record_extraction_build", _boom)
    h._account_build("ns", 1, 1, 1, 1)  # never raises into the write path


def test_the_handler_prices_each_written_row_from_the_requests_accumulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.extract import handler as h
    from server.observability import cost_accumulator, metrics

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(metrics, "record_extraction_build", lambda **kw: None)
    monkeypatch.setattr(metrics, "record_extract_cost_per_row", lambda **kw: seen.append(kw))

    def request() -> None:
        cost_accumulator.begin_op().cost_usd = 0.3
        h._account_build("ns", 1, 1, 3, 1)

    # Its own context, so the accumulator it binds ends with it.
    contextvars.copy_context().run(request)
    assert seen == [{"namespace_kind": "unclassified", "cost_usd": 0.3, "rows": 3}]
    h._account_build("ns", 1, 1, 3, 1)  # no accumulator outside a request: nothing priced
    assert len(seen) == 1


def test_bench_efficiency_summary_and_usage_sink() -> None:
    from bench import longmemeval as b

    r = b.ItemResult(
        question_id="q",
        question_type="t",
        question="?",
        gold_answer="a",
        top_k_excerpts=[],
        recalled=True,
        build_tokens_in=15000,
        build_tokens_out=800,
        build_calls=3,
        memories_written=12,
        build_seconds=30.0,
        qa_tokens_in=4000,
        qa_tokens_out=50,
        qa_calls=3,
        qa_seconds=4.0,
    )
    eff = b.efficiency_summary([r, r])
    assert (
        eff["build_tokens_k"] == 31.6 and eff["build_calls"] == 6 and eff["memories_written"] == 24
    )
    assert (
        eff["tokens_per_memory"] == 1250.0
        and eff["qa_tokens_k"] == 8.1
        and eff["total_tokens_k"] == 39.7
    )
    assert eff["per_item"] == {
        "build_tokens_k": 15.8,
        "qa_tokens_k": 4.05,
        "build_seconds": 30.0,
        "qa_seconds": 4.0,
    }
    # A second answering arm's rows built nothing; they must not halve the
    # per-history build cost.
    reader = b.ItemResult(
        question_id="q",
        question_type="t",
        question="?",
        gold_answer="a",
        top_k_excerpts=[],
        recalled=True,
        qa_tokens_in=4000,
        qa_tokens_out=50,
        qa_calls=1,
        qa_seconds=4.0,
    )
    mixed = b.efficiency_summary([r, reader])["per_item"]
    assert mixed["build_tokens_k"] == 15.8 and mixed["build_seconds"] == 30.0
    assert (
        b.efficiency_summary([])["items"] == 0
        and b.efficiency_summary([])["tokens_per_memory"] == 0.0
    )

    sink: dict[str, int] = {}
    b._record_usage(sink, {"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    b._record_usage(sink, {"usage": {"prompt_tokens": 5}})
    b._record_usage(sink, {"no": "usage"})
    b._record_usage(None, {"usage": {"prompt_tokens": 99}})
    assert sink == {"tin": 15, "tout": 2, "calls": 3}


def test_extraction_yield_messages_label_is_a_fixed_range_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's batch size must not reach the histogram as a raw number:
    every value maps to one of six ranges."""
    from server.observability import metrics

    yld = _Instr()
    monkeypatch.setattr(metrics, "_memory_build_tokens", None)
    monkeypatch.setattr(metrics, "_memory_extraction_yield", yld)
    for n in (-3, *range(1500)):
        metrics.record_extraction_build(
            namespace="ns", tokens_in=10, tokens_out=1, written=1, messages=n
        )
    seen = {a["messages"] for _, a in yld.records}
    assert seen == {"0", "1", "2-5", "6-20", "21-100", "101+"}
    edges = {1: "1", 2: "2-5", 5: "2-5", 6: "6-20", 20: "6-20", 21: "21-100", 101: "101+"}
    assert {n: metrics.messages_bucket(n) for n in edges} == edges
