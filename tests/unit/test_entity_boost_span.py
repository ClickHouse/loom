"""The entity boost's statement runs under a
`ch.query.entity_vectors` child span (it is a task beside the vector legs,
charged to no `stage_ms` stage, so the span is the only place its wall is
attributable); a request with no query entities issues no statement and no
span."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.search import ann_entity_boosts as mod


@pytest.fixture
def in_memory_spans(monkeypatch: pytest.MonkeyPatch) -> Any:
    from opentelemetry import trace as _trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real_get_tracer = _trace.get_tracer

    def _get_tracer(name: str, *a: Any, **kw: Any) -> Any:
        if name == "loom.handlers":
            return provider.get_tracer(name)
        return real_get_tracer(name, *a, **kw)

    monkeypatch.setattr(_trace, "get_tracer", _get_tracer)
    return exporter


class _Embedder:
    model_name = "m"
    dim = 3

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append(params)
        return [{"namespace": "ns", "memory_key": "k1", "distance": 0.1}]


async def test_each_entity_statement_runs_under_its_own_span(
    in_memory_spans: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = _Client()
    monkeypatch.setattr(mod, "get_client", lambda: ch)
    boosts = await mod._ann_entity_boosts(
        org="o",
        namespaces=["ns", "ns2"],
        agent="a",
        user_id=None,
        query_entities=["Alice", "Bob"],
        embedder=_Embedder(),
    )
    spans = in_memory_spans.get_finished_spans()
    assert [s.name for s in spans] == ["ch.query.entity_vectors"] * 2
    assert dict(spans[0].attributes) == {
        "op": "entity_boost",
        "entities": 2,
        "namespaces": 2,
    }
    assert len(ch.calls) == 2 and ch.calls[0]["uid"] == "-"
    assert boosts == {("ns", "k1"): 0.5}


async def test_no_query_entities_means_no_statement_and_no_span(
    in_memory_spans: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = _Client()
    monkeypatch.setattr(mod, "get_client", lambda: ch)
    boosts = await mod._ann_entity_boosts(
        org="o", namespaces=["ns"], agent="a", user_id="u", query_entities=[], embedder=_Embedder()
    )
    assert boosts == {}
    assert ch.calls == []
    assert in_memory_spans.get_finished_spans() == ()
