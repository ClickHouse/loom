"""Worker-plane telemetry: `record_pass` emits metrics + a root span, the
supervisor publishes failure streaks, worker identity reaches provider spans,
and worker LLM spend reaches `loom.cost.usd`.

An in-process snapshot alone would let a worker failing every other pass look
healthy whenever you happened to look, keep pass duration nowhere, leave the
supervisor's DEGRADED line a log and nothing else, and miss every dollar of
`worker.*` spend from the spend panels.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.observability import genai, metrics, metrics_gauges
from server.workers import status as st


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, Any]]] = []

    def add(self, value: Any, attributes: Any = None) -> None:
        self.calls.append((value, dict(attributes or {})))

    def record(self, value: Any, attributes: Any = None) -> None:
        self.calls.append((value, dict(attributes or {})))


@pytest.fixture
def worker_instruments(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Recorder]:
    recs = {"_worker_passes": _Recorder(), "_worker_pass_duration": _Recorder()}
    for n, r in recs.items():
        monkeypatch.setattr(metrics, n, r)
    return recs


@pytest.fixture
def in_memory_spans(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A private TracerProvider (never installed globally — the global one is
    write-once per process) routed through `worker_pass_span`."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    from opentelemetry import trace as _trace

    real_get_tracer = _trace.get_tracer

    def _get_tracer(name: str, *a: Any, **kw: Any) -> Any:
        if name in ("loom.workers", "loom.cli"):
            return provider.get_tracer(name)
        return real_get_tracer(name, *a, **kw)

    monkeypatch.setattr(_trace, "get_tracer", _get_tracer)
    return exporter


def test_record_pass_success_emits_counter_histogram_and_span(
    worker_instruments: dict[str, _Recorder], in_memory_spans: Any
) -> None:
    st.get_registry().reset()
    genai.set_identity()
    with st.record_pass("code_ingest") as rec:
        rec.outcome = {"commits": 3, "head": "abc123", "skipped": ["x"], "note": ""}

    (count,) = worker_instruments["_worker_passes"].calls
    assert count == (1, {"worker": "code_ingest", "outcome": "success"})
    ((dur, attrs),) = worker_instruments["_worker_pass_duration"].calls
    assert attrs == {"worker": "code_ingest"} and dur >= 0

    (span,) = in_memory_spans.get_finished_spans()
    assert span.name == "worker.code_ingest.pass"
    assert span.parent is None  # a root: no request exists here
    a = dict(span.attributes or {})
    assert a["loom.worker"] == "code_ingest"
    assert a["loom.operation"] == "worker.code_ingest"
    assert a["loom.worker.success"] is True
    assert a["loom.worker.outcome.commits"] == 3
    assert a["loom.worker.outcome.head"] == "abc123"
    assert "loom.worker.outcome.skipped" not in a  # lists never become attributes
    assert "loom.worker.outcome.note" not in a  # nor empty strings
    # Identity for provider spans made inside the pass.
    assert (genai._identity.get() or {}).get("operation") == "worker.code_ingest"
    genai.set_identity()


def test_record_pass_failure_emits_failure_and_error_status(
    worker_instruments: dict[str, _Recorder], in_memory_spans: Any
) -> None:
    from opentelemetry.trace import StatusCode

    st.get_registry().reset()
    with pytest.raises(RuntimeError):
        with st.record_pass("code_summary"):
            raise RuntimeError("provider down")

    (count,) = worker_instruments["_worker_passes"].calls
    assert count[1] == {"worker": "code_summary", "outcome": "failure"}
    assert len(worker_instruments["_worker_pass_duration"].calls) == 1
    (span,) = in_memory_spans.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert "RuntimeError" in (span.status.description or "")
    assert any(e.name == "exception" for e in span.events)
    # The registry contract is unchanged.
    snap = st.get_registry().snapshot()["code_summary"]
    assert snap["consecutive_failures"] == 1 and "provider down" in snap["last_error"]
    genai.set_identity()


def test_record_pass_survives_broken_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A telemetry bug must never fail a pass or eat its exception."""
    from server.observability import otel

    def _boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("otel broke")

    monkeypatch.setattr(otel, "worker_pass_span", _boom)
    monkeypatch.setattr(metrics, "record_worker_pass", _boom)
    st.get_registry().reset()
    with st.record_pass("embedding_worker") as rec:
        rec.outcome = {"ok": True}
    assert st.get_registry().snapshot()["embedding_worker"]["last_outcome"] == {"ok": True}
    with pytest.raises(ValueError):
        with st.record_pass("embedding_worker"):
            raise ValueError("real failure")
    genai.set_identity()


def test_record_worker_pass_no_op_without_instruments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "_worker_passes", None)
    monkeypatch.setattr(metrics, "_worker_pass_duration", None)
    metrics.record_worker_pass("x", success=True, duration_ms=1)  # must not raise


# --- identity ----------------------------------------------------------------


def test_worker_identity_merges_and_yields_to_a_request() -> None:
    genai.set_identity()
    genai.set_worker_identity("code_ingest")
    assert genai._identity.get() == {"operation": "worker.code_ingest"}
    # Per-repo call adds tenant without dropping the operation.
    genai.set_worker_identity("code_ingest", org="acme", namespace="code-x")
    assert genai._identity.get() == {
        "operation": "worker.code_ingest",
        "org": "acme",
        "namespace": "code-x",
    }
    # Inside a request (code.refresh wait=true) the request's identity —
    # consent-resolved capture level included — must win.
    genai.set_identity(
        org="acme", namespace="code-x", operation="code.refresh", user_id="u1", capture_level="off"
    )
    genai.set_worker_identity("code_ingest", org="other", namespace="other")
    ident = genai._identity.get() or {}
    assert ident["operation"] == "code.refresh"
    assert ident["org"] == "acme" and ident["capture_level"] == "off"
    genai.set_identity()


def test_a_pass_does_not_inherit_the_previous_pass_tenant() -> None:
    """Pass N's last per-item call set org A; pass N+1 starts with no tenant, so
    the spans it opens before its own first item carry no org A tag."""
    genai.set_identity()
    with st.record_pass("code_summary"):
        genai.set_worker_identity("code_summary", org="acme", namespace="code-a")
    with st.record_pass("code_summary"):
        assert genai._identity.get() == {"operation": "worker.code_summary"}
    # Inside a request the request's identity still wins.
    genai.set_identity(org="acme", namespace="code-x", operation="code.refresh")
    with st.record_pass("code_ingest"):
        assert (genai._identity.get() or {})["operation"] == "code.refresh"
    genai.set_identity()


# --- supervisor streak gauge -------------------------------------------------


@pytest.mark.asyncio
async def test_supervisor_publishes_consecutive_failures() -> None:
    from server.workers.registry import WorkerSpec
    from server.workers.supervisor import WorkerSupervisor

    calls = {"n": 0}

    async def _flaky() -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("upstream down")

    sup = WorkerSupervisor()
    seen: list[int] = []

    async def _sleep(_seconds: float) -> None:
        seen.append(metrics_gauges.worker_consecutive_failures().get("flaky", -1))
        if calls["n"] >= 3:
            sup._stopping.set()

    sup._sleep_or_drain = _sleep  # type: ignore[method-assign]
    spec = WorkerSpec("flaky", 0.01, _flaky)
    genai.set_identity()
    await asyncio.wait_for(sup._supervise(spec), timeout=5)

    # 1 after the first failure, 2 after the second, 0 once it recovers.
    assert seen == [1, 2, 0]
    assert metrics_gauges.worker_consecutive_failures()["flaky"] == 0
    # The supervised task's provider spans are attributed to the worker.
    assert (genai._identity.get() or {}).get("operation") == "worker.flaky"
    genai.set_identity()


def test_consecutive_failures_gauge_reads_the_streaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics_gauges, "_worker_failure_streaks", {})
    metrics_gauges.set_worker_consecutive_failures("a", 3)
    metrics_gauges.set_worker_consecutive_failures("b", 0)

    class _Meter:
        gauges: dict[str, Any] = {}

        def create_observable_gauge(self, *, name: str, callbacks: Any, **_: Any) -> None:
            self.gauges[name] = callbacks[0]

        def create_counter(self, **_: Any) -> _Recorder:
            return _Recorder()

        def create_histogram(self, **_: Any) -> _Recorder:
            return _Recorder()

    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict, code_probe=dict)
    obs = meter.gauges["loom.worker.consecutive_failures"](None)
    assert {(o.value, o.attributes["worker"]) for o in obs} == {(3, "a"), (0, "b")}


# --- worker cost → metrics ---------------------------------------------------


def test_worker_cost_event_reaches_record_cost_and_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import cost_accumulator
    from server.observability import events as ev
    from server.workers.cost_event import emit_worker_cost_event_for

    emitted: list[Any] = []

    class _Emitter:
        def emit(self, e: Any) -> None:
            emitted.append(e)

    monkeypatch.setattr(ev, "get_emitter", lambda: _Emitter())
    cost_rec, tok_rec = _Recorder(), _Recorder()
    monkeypatch.setattr(metrics, "_op_cost", cost_rec)
    monkeypatch.setattr(metrics, "_op_tokens", tok_rec)

    acc = cost_accumulator.begin_op()
    acc.cost_usd, acc.embed_tokens = 0.0125, 400
    acc.llm_input_tokens, acc.llm_output_tokens = 1000, 50

    import time

    emit_worker_cost_event_for(
        org="acme", namespace="code-x", operation="worker.code_ingest", started=time.monotonic()
    )

    (event,) = emitted
    assert event.operation == "worker.code_ingest" and event.upstream_cost_usd == 0.0125
    assert cost_rec.calls == [
        (0.0125, {"loom.operation": "worker.code_ingest", "loom.namespace": "code-x"})
    ]
    kinds = {c[1]["kind"]: c[0] for c in tok_rec.calls}
    assert kinds == {"embed": 400, "llm_input": 1000, "llm_output": 50}
    # Drained: a transport reading the same accumulator afterwards (code.refresh
    # wait=true shares the request context) finds nothing left to count.
    assert (acc.cost_usd, acc.embed_tokens, acc.llm_input_tokens, acc.llm_output_tokens) == (
        0.0,
        0,
        0,
        0,
    )
    emit_worker_cost_event_for(
        org="acme", namespace="code-x", operation="worker.code_ingest", started=time.monotonic()
    )
    assert len(emitted) == 1 and len(cost_rec.calls) == 1


# --- Langfuse: pass roots stay in ClickStack ---------------------------------


def test_langfuse_filter_drops_worker_pass_roots_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """25 workers on short intervals would each become an empty Langfuse trace
    per pass. The root pass span is dropped; the generations it makes (children,
    and any other root) still go through."""
    from server.observability import otel_langfuse

    monkeypatch.setattr(otel_langfuse.settings, "langfuse_sample_rate", 1.0)
    monkeypatch.setattr(otel_langfuse.settings, "langfuse_span_denylist", "")
    f = otel_langfuse._LangfuseSpanFilter()

    class _Span:
        def __init__(self, name: str, parent: Any) -> None:
            self.name, self.parent = name, parent

        def get_span_context(self) -> Any:
            raise AssertionError("rate=1.0 must decide before sampling")

    assert f.keep(_Span("worker.code_ingest.pass", None)) is False
    assert f.keep(_Span("llm.code_describe", "parent")) is True
    assert f.keep(_Span("worker.code_ingest.pass", "parent")) is True  # not a root
    assert f.keep(_Span("cli.code.scip-full", None)) is True  # CLI roots are rare; keep
