"""The Langfuse OTLP span processor: filtering, and trace-consistent sampling.

This processor is the only thing standing between Langfuse Cloud and ~1M spans a
day, and a mistake in it is invisible — you get a plausible-looking but wrong
subset. So the rules are pinned here rather than checked by eye.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.observability import otel, otel_langfuse


class _FakeCtx:
    def __init__(self, trace_id: int) -> None:
        self.trace_id = trace_id


class _FakeSpan:
    def __init__(self, name: str, trace_id: int = 1, parent: Any = "p") -> None:
        self.name = name
        self.parent = parent
        self._ctx = _FakeCtx(trace_id)

    def get_span_context(self) -> _FakeCtx:
        return self._ctx


class _RecordingInner:
    def __init__(self) -> None:
        self.ended: list[str] = []
        self.started = 0
        self.flushed = False
        self.shut = False

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        self.started += 1

    def on_end(self, span: Any) -> None:
        self.ended.append(span.name)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.flushed = True
        return True

    def shutdown(self) -> None:
        self.shut = True


@pytest.fixture(autouse=True)
def _no_request_identity() -> Any:
    """Each test starts outside a request; another file's identity must not leak in."""
    from server.observability import genai

    token = genai._identity.set({})
    yield
    genai._identity.reset(token)


def _proc(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    defaults = {
        "langfuse_sample_rate": 1.0,
        "langfuse_span_denylist": "ch.query,ch.insert",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        monkeypatch.setattr(otel_langfuse.settings, k, v, raising=False)
    inner = _RecordingInner()
    return otel_langfuse._langfuse_processor_class()(
        inner, otel_langfuse._LangfuseSpanFilter()
    ), inner


def test_infrastructure_spans_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """ClickHouse spans belong in ClickStack, where they are cheap."""
    proc, inner = _proc(monkeypatch)
    for name in ("ch.query.memory", "ch.insert.memory", "llm.hyde", "memory.search"):
        proc.on_end(_FakeSpan(name))
    assert inner.ended == ["llm.hyde", "memory.search"]


def test_root_embeddings_are_dropped_but_request_ones_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The volume filter must not take the embeddings that matter with it.

    A request-path embed nests inside a handler span, so it has a parent. The
    embedding worker calls the embedder directly, so its spans are trace roots —
    they can outnumber LLM spans by more than an order of magnitude. Dropping roots
    removes the backfill flood and keeps the embedding a user's search paid for.
    """
    proc, inner = _proc(monkeypatch)
    proc.on_end(_FakeSpan("embed.provider", parent=None))  # worker backfill
    proc.on_end(_FakeSpan("embed.provider", parent="handler-span"))  # a search
    assert inner.ended == ["embed.provider"]
    assert proc.dropped == 1
    assert proc.forwarded == 1


def test_sampling_is_all_or_nothing_per_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every span of one trace must reach the same verdict.

    A per-span coin flip would ship orphaned children — the waterfall would show
    a search whose LLM legs are missing at random, and the per-trace cost sum,
    which is the whole point of this integration, would be wrong by a random
    fraction. So the decision is a pure function of the trace id.
    """
    proc, inner = _proc(monkeypatch, langfuse_sample_rate=0.5)
    for trace_id in (7, 1 << 100, 12345678901234567890):
        spans = [
            _FakeSpan("memory.search", trace_id=trace_id),
            _FakeSpan("llm.hyde", trace_id=trace_id),
            _FakeSpan("embed.provider", trace_id=trace_id),
        ]
        verdicts = {proc._filter.keep(s) for s in spans}
        assert len(verdicts) == 1, (
            f"trace {trace_id} split across the sampling decision; its waterfall "
            f"would be missing children at random"
        )


def test_sample_rate_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 drops everything, 1 keeps everything, and out-of-range is clamped."""
    keep_all, _ = _proc(monkeypatch, langfuse_sample_rate=1.0)
    drop_all, _ = _proc(monkeypatch, langfuse_sample_rate=0.0)
    clamped_hi, _ = _proc(monkeypatch, langfuse_sample_rate=7.5)
    clamped_lo, _ = _proc(monkeypatch, langfuse_sample_rate=-3.0)
    span = _FakeSpan("llm.hyde", trace_id=0xDEADBEEFCAFEBABE)
    assert keep_all._filter.keep(span) is True
    assert drop_all._filter.keep(span) is False
    assert clamped_hi._filter.keep(span) is True
    assert clamped_lo._filter.keep(span) is False


def test_sampling_is_roughly_the_requested_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against a threshold that is off by orders of magnitude.

    `int(rate * (1 << 64))` compared against the low 64 bits is easy to get wrong
    in a way that still "works" — e.g. masking the wrong half of the id yields a
    rate of ~0 or ~1 while looking correct.
    """
    proc, _ = _proc(monkeypatch, langfuse_sample_rate=0.25)
    # Spread the ids across the 64-bit space rather than using 0..999, which
    # would all fall under any non-trivial threshold.
    kept = sum(
        proc._filter.keep(
            _FakeSpan("llm.hyde", trace_id=(i * 0x9E3779B97F4A7C15) & ((1 << 128) - 1))
        )
        for i in range(2000)
    )
    assert 400 < kept < 600, f"expected ~500/2000 at rate 0.25, got {kept}"


def test_filtering_failure_still_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug in the filter must not silently stop export.

    The `except` in `on_end` swallows filter errors and falls through to the
    inner processor — losing telemetry is the worse outcome, and a span that
    should have been dropped only costs money.
    """
    proc, inner = _proc(monkeypatch)

    class _Exploding:
        name = "llm.hyde"
        parent = None

        def get_span_context(self) -> Any:
            raise RuntimeError("boom")

    monkeypatch.setattr(proc._filter, "rate", 0.5)
    proc.on_end(_Exploding())
    assert inner.ended == ["llm.hyde"]


def test_lifecycle_is_delegated(monkeypatch: pytest.MonkeyPatch) -> None:
    """shutdown/force_flush must reach the inner processor.

    `shutdown()` is what flushes the queue on SIGTERM; a processor that swallows
    it loses every buffered span on each deploy.
    """
    proc, inner = _proc(monkeypatch)
    proc.on_start(_FakeSpan("x"), None)
    assert proc.force_flush(1000) is True
    proc.shutdown()
    assert inner.started == 1
    assert inner.flushed is True
    assert inner.shut is True


def test_otlp_auth_is_basic_not_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Langfuse's OTLP endpoint takes HTTP Basic on `pk:sk`.

    A bearer token gets a 401, and the failure mode is silent — the
    BatchSpanProcessor logs and drops. Pinned because it is the single easiest
    thing to get wrong here.
    """
    monkeypatch.setattr(
        otel_langfuse.settings, "langfuse_admin_token", "pk-lf-abc:sk-lf-xyz", raising=False
    )
    headers = otel_langfuse._langfuse_otlp_headers()
    assert headers is not None
    assert headers["Authorization"].startswith("Basic ")
    import base64

    decoded = base64.b64decode(headers["Authorization"].split()[1]).decode()
    assert decoded == "pk-lf-abc:sk-lf-xyz"
    # Pins the ingest schema; without it the GenAI attributes are not read as usage.
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_malformed_token_yields_no_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token without the `:` cannot authenticate; fail closed, not with a 401 loop."""
    monkeypatch.setattr(otel_langfuse.settings, "langfuse_admin_token", "pk-lf-only", raising=False)
    assert otel_langfuse._langfuse_otlp_headers() is None


def test_endpoint_is_the_otlp_traces_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        otel_langfuse.settings, "langfuse_url", "https://us.cloud.langfuse.com/", raising=False
    )
    assert (
        otel_langfuse._langfuse_otlp_endpoint()
        == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    )
    monkeypatch.setattr(otel_langfuse.settings, "langfuse_url", "", raising=False)
    assert otel_langfuse._langfuse_otlp_endpoint() is None


def test_unconfigured_by_default() -> None:
    """Sending span attributes to a third party must be an explicit decision:
    the processor attaches only once a Langfuse URL and key pair are set.
    Asserts the FIELD defaults, not `Settings()`, which reads the local `.env`."""
    from server.settings import Settings

    assert Settings.model_fields["langfuse_url"].default == ""
    assert Settings.model_fields["langfuse_admin_token"].default == ""


def test_export_stays_off_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unconfigured must not attach a processor that 401s forever."""
    monkeypatch.setattr(otel_langfuse.settings, "langfuse_url", "", raising=False)

    class _Provider:
        def __init__(self) -> None:
            self.added: list[Any] = []

        def add_span_processor(self, p: Any) -> None:
            self.added.append(p)

    provider = _Provider()
    assert otel_langfuse._add_langfuse_processor(provider) is None
    assert provider.added == []


def test_processor_is_a_real_span_processor_subclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It must subclass the SDK's SpanProcessor, not duck-type its four methods.

    The SDK calls PRIVATE hooks on processors — `_on_ending` among them — and
    reaches them through the multi-processor on every span end. A stand-in
    implementing only the public interface raised AttributeError on every single
    span: it surfaced as "embedding dim probe failed ... falling back to
    settings.embedding_dim=3072", i.e. an observability change silently degrading
    the embedding path. Subclassing inherits present and future hooks.
    """
    from opentelemetry.sdk.trace import SpanProcessor

    cls = otel_langfuse._langfuse_processor_class()
    assert issubclass(cls, SpanProcessor)

    proc, inner = _proc(monkeypatch)
    # Every hook the SDK may call must resolve, not just the four we wrote.
    for hook in ("on_start", "on_end", "shutdown", "force_flush", "_on_ending"):
        assert callable(getattr(proc, hook)), f"missing SDK hook {hook}"
    # And the inherited one must be safe to call.
    proc._on_ending(_FakeSpan("llm.hyde"))


def test_http_client_spans_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare-verb spans are httpx client spans, not requests.

    They are the outbound call to OpenAI, duplicating the generation span that
    already wraps it — and at trace root (workers) each becomes its own nameless
    single-span trace. The FastAPI server span, which IS the request, keeps its
    path and survives.
    """
    proc, inner = _proc(monkeypatch)
    for name in ("POST", "GET", "POST /v1/memory.search"):
        proc.on_end(_FakeSpan(name))
    assert inner.ended == ["POST /v1/memory.search"]


def test_asgi_and_health_spans_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport mechanics and load-balancer polling are not LLM observability.

    Health polls would otherwise dominate the trace list, and every request
    contributes three ASGI lifecycle sub-spans.
    """
    monkeypatch.setattr(
        otel_langfuse.settings,
        "langfuse_span_denylist",
        "ch.query,ch.insert,GET /v1/health,POST /v1/health",
        raising=False,
    )
    proc, inner = _proc(
        monkeypatch,
        langfuse_span_denylist="ch.query,ch.insert,GET /v1/health,POST /v1/health",
    )
    for name in (
        "GET /v1/health",
        "GET /v1/health http send",
        "POST /v1/memory.search http receive",
        "memory.search",
    ):
        proc.on_end(_FakeSpan(name))
    assert inner.ended == ["memory.search"]


def test_context_only_model_attribute_is_not_a_generation() -> None:
    """`traced(..., model=...)` must not become a zero-token generation.

    Langfuse turns any span with a `model` attribute into a GENERATION.
    `traced("embed.compute.query", model=...)` passes the model purely as
    context, and those arrived as generations with 0 tokens and $0 — inflating
    the generation count with empty rows and dragging every per-generation
    average down.
    """
    captured: dict[str, Any] = {}

    class _Span:
        def set_attribute(self, k: str, v: Any) -> None:
            captured[k] = v

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    class _Tracer:
        def start_as_current_span(self, name: str) -> Any:
            return _Span()

    import opentelemetry.trace as _t

    orig = _t.get_tracer
    _t.get_tracer = lambda *a, **k: _Tracer()  # type: ignore[assignment]
    try:
        with otel.traced("embed.compute.query", model="text-embedding-3-large"):
            pass
    finally:
        _t.get_tracer = orig  # type: ignore[assignment]
    assert captured.get("langfuse.observation.type") == "span"


def test_no_processor_method_can_fail_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry must never be able to break the thing it observes.

    The OTel SDK calls `_on_ending` and `on_end` from `Span.end()` with NO guard of
    its own, so anything raised here lands in whatever ended the span — a request
    handler. A processor that duck-typed the interface would lack the SDK's private
    `_on_ending`, and `Span.end()` would raise AttributeError on every span.

    Subclassing covers that instance. This test covers the CLASS: an inner processor
    that raises on every single method must not propagate anything.
    """

    class _Hostile:
        """Raises on everything the SDK might call."""

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            raise RuntimeError("on_start exploded")

        def _on_ending(self, span: Any) -> None:
            raise RuntimeError("_on_ending exploded")

        def on_end(self, span: Any) -> None:
            raise RuntimeError("on_end exploded")

        def shutdown(self) -> None:
            raise RuntimeError("shutdown exploded")

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            raise RuntimeError("force_flush exploded")

    for k, v in {
        "langfuse_sample_rate": 1.0,
        "langfuse_span_denylist": "",
    }.items():
        monkeypatch.setattr(otel_langfuse.settings, k, v, raising=False)

    proc = otel_langfuse._langfuse_processor_class()(
        _Hostile(), otel_langfuse._LangfuseSpanFilter()
    )
    span = _FakeSpan("memory.search")

    # Not one of these may raise.
    proc.on_start(span, None)
    proc._on_ending(span)
    proc.on_end(span)
    proc.shutdown()
    assert proc.force_flush(1000) is False, "a failed flush reports False, not raises"

    # Contained is not invisible: the failures are counted so a broken processor
    # is diagnosable rather than silently dropping everything.
    assert proc.errors == 5, f"expected 5 suppressed errors, got {proc.errors}"


def test_a_raising_filter_does_not_stop_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in the FILTER must not also cost us the span.

    Failing to drop a span costs money; failing to export it costs the evidence.
    Export wins.
    """
    proc, inner = _proc(monkeypatch)

    class _BadFilter:
        def keep(self, span: Any) -> bool:
            raise RuntimeError("filter exploded")

    monkeypatch.setattr(proc, "_filter", _BadFilter())
    proc.on_end(_FakeSpan("llm.hyde"))
    assert inner.ended == ["llm.hyde"]
    assert proc.errors == 1


class _WritableSpan(_FakeSpan):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def test_every_forwarded_span_carries_the_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Langfuse 4 reads user, session and tags per observation, not from the root; the
    tenant keys come along so ClickStack's erasure org guard can read them."""
    from server.observability import genai

    proc, inner = _proc(monkeypatch)
    genai.set_identity(
        org="o", namespace="ns", user_id="u1", session_id="s1", operation="memory.search"
    )
    span = _WritableSpan("search.anchor_pool")
    proc.on_start(span, None)
    assert span.attributes == {
        "loom.operation": "memory.search",
        "loom.org": "o",
        "loom.namespace": "ns",
        "loom.user_id": "u1",
        "langfuse.session.id": "s1",
        "langfuse.user.id": "u1",
        "langfuse.trace.tags": ["org:o", "namespace:ns"],
        "langfuse.trace.name": "memory.search",
    }
    assert inner.started == 1


def test_a_span_that_rejects_attributes_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import genai

    proc, inner = _proc(monkeypatch)
    genai.set_identity(org="o", namespace="ns")
    proc.on_start(_FakeSpan("x"), None)  # no set_attribute
    assert inner.started == 1 and proc.errors == 1
