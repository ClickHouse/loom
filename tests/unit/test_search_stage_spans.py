"""The additive pool arms (sub-queries, set expansion, anchor
pool, key match, digest descent) run under a `search.<name>` child span that
carries what the arm added to the pool, and the same count rides
`retrieval_work.<name>_added`. An arm that will not run is awaited without a
span, so a default search does not grow five empty spans."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app.search.telemetry import _staged


@pytest.fixture
def in_memory_spans(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A private TracerProvider routed through `traced()`'s tracer name (the
    global provider is write-once per process, so it is never installed)."""
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


def _ctx() -> Any:
    return SimpleNamespace(all_hits=["a", "b"], namespaces=["ns", "ns2"], actual_work={})


async def _adds(ctx: Any, n: int) -> None:
    ctx.all_hits.extend(range(n))


async def test_an_active_arm_gets_a_span_with_its_result_count(in_memory_spans: Any) -> None:
    ctx = _ctx()
    await _staged(ctx, "anchor_pool", _adds(ctx, 3), active=True)
    (span,) = in_memory_spans.get_finished_spans()
    assert span.name == "search.anchor_pool"
    assert dict(span.attributes) == {
        "loom.search.namespaces": 2,
        "langfuse.observation.type": "retriever",
        "loom.search.added": 3,
        "loom.search.pool": 5,
    }
    assert ctx.actual_work == {"anchor_pool_added": 3}


async def test_an_inactive_arm_is_awaited_without_a_span(in_memory_spans: Any) -> None:
    ctx = _ctx()
    await _staged(ctx, "key_match", _adds(ctx, 1), active=False)
    assert in_memory_spans.get_finished_spans() == ()
    assert ctx.all_hits == ["a", "b", 0]
    assert ctx.actual_work == {}


async def test_a_failing_arm_still_closes_its_span_and_counts_what_landed(
    in_memory_spans: Any,
) -> None:
    ctx = _ctx()

    async def _adds_then_fails() -> None:
        ctx.all_hits.append("x")
        raise RuntimeError("leg failed")

    with pytest.raises(RuntimeError):
        await _staged(ctx, "digest_descent", _adds_then_fails(), active=True)
    (span,) = in_memory_spans.get_finished_spans()
    assert span.name == "search.digest_descent"
    assert span.attributes["loom.search.added"] == 1
    assert ctx.actual_work["digest_descent_added"] == 1


async def test_without_a_recording_span_the_arm_still_runs_and_counts() -> None:
    # No provider at all: traced() yields a non-recording span; the count
    # still reaches retrieval_work.
    ctx = _ctx()
    await _staged(ctx, "sub_queries", _adds(ctx, 2), active=True)
    assert ctx.actual_work == {"sub_queries_added": 2}
