"""Private work leaves ordinary diagnostic state unchanged, including with a parent span."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from server.core import ch_request_cost, diagnostics, request_work
from server.observability import cost_accumulator, genai, metrics, otel
from server.observability.events import AgentEvent, EventEmitter

MIRROR = "acl-" + "a" * 24


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request_work, "_current", ContextVar("test_work", default=None))
    monkeypatch.setattr(cost_accumulator, "_current", ContextVar("test_cost", default=None))


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Any:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        trace,
        "get_tracer",
        lambda name, *args, tracer_provider=None, **kwargs: provider.get_tracer(
            name, *args, **kwargs
        ),
    )
    yield provider.get_tracer("test"), exporter
    provider.shutdown()


def test_private_manual_spans_do_not_record_or_contaminate_an_existing_parent(spans: Any) -> None:
    tracer, exporter = spans
    with tracer.start_as_current_span("parent") as parent:
        try:
            with diagnostics.private_work():
                with otel.traced("private.query", private="secret"):
                    with genai.embedding_span(provider="fake", model="fake-ngram-1024") as emb:
                        emb.record_usage(billed_tokens=17, batch_size=3)
                        raise ValueError("private title")
        except ValueError:
            pass
        assert not parent.attributes
        assert not parent.events
    assert [s.name for s in exporter.get_finished_spans()] == ["parent"]


def test_private_database_summary_never_reaches_parent_or_shared_work_bag(spans: Any) -> None:
    tracer, exporter = spans
    bag = request_work.begin()
    result = SimpleNamespace(summary={"read_rows": 1000, "read_bytes": 9000, "elapsed_ns": 5000000})
    with tracer.start_as_current_span("parent") as parent:
        with diagnostics.private_work():
            assert ch_request_cost.note_result_summary(result) == {}
        assert not parent.attributes
    assert bag.ch_statements == bag.ch_read_bytes == 0
    ch_request_cost.note_result_summary(result)
    assert bag.ch_statements == 1 and bag.ch_read_bytes == 9000


def test_private_calls_do_not_publish_cost_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    counter = SimpleNamespace(add=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(metrics, "_op_cost", counter)
    monkeypatch.setattr(metrics, "_op_tokens", counter)
    args = dict(
        namespace=MIRROR, cost_usd=1.0, embed_tokens=20, llm_input_tokens=30, llm_output_tokens=4
    )
    with diagnostics.private_work():
        metrics.record_cost("worker.source", **args)
    assert calls == []
    metrics.record_cost("worker.source", **(args | {"namespace": "team"}))
    assert len(calls) == 4


@pytest.mark.parametrize("namespace", ["team", MIRROR])
def test_private_events_never_enter_shared_queue_or_its_drop_statistics(namespace: str) -> None:
    emitter = EventEmitter(queue=asyncio.Queue(maxsize=1))
    event = AgentEvent(
        ts=datetime.now(UTC),
        org="org",
        namespace=namespace,
        agent="-",
        user_id="-",
        session_id="-",
        operation="worker.source",
        request_id="",
        latency_ms=1,
        success=True,
        kind="worker",
    )
    before = emitter.stats()
    with diagnostics.private_work():
        emitter.emit(event)
        emitter.emit(event)
    assert emitter.stats() == before
    if namespace == MIRROR:
        emitter.emit(event)
        assert emitter.stats() == before


def test_private_policy_prevents_request_scoped_embedding_cache_identity() -> None:
    token = genai._identity.set(None)
    try:
        genai.set_identity(org="org", namespace="team", operation="memory.set")
        assert genai.request_org() == "org"
        with diagnostics.private_work():
            assert genai.request_org() == ""
        assert genai.request_org() == "org"
    finally:
        genai._identity.reset(token)


async def test_nested_private_policy_restores_after_failure_and_cancellation() -> None:
    assert not diagnostics.codes_only()
    with pytest.raises(asyncio.CancelledError):
        with diagnostics.private_work():
            with diagnostics.private_work():
                assert diagnostics.codes_only()
            assert diagnostics.codes_only()
            raise asyncio.CancelledError
    assert not diagnostics.codes_only()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_private_spend_is_written_under_mirror_without_shared_accounting(
    cancelled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from server.observability import events
    from server.workers.cost_event import write_private_cost_event_for

    rows: list[dict[str, Any]] = []

    async def insert(table: str, batch: list[dict[str, Any]], **kwargs: Any) -> None:
        assert diagnostics.codes_only()
        assert table == "loom.agent_events"
        rows.extend(batch)

    monkeypatch.setattr(events, "get_client", lambda: SimpleNamespace(insert=insert))
    outer = cost_accumulator.begin_op()
    cost_accumulator.report_embed("fake-ngram-1024", 2)
    before = events.get_emitter().stats()
    try:
        with diagnostics.private_work(), cost_accumulator.isolated_op():
            try:
                cost_accumulator.report_embed("fake-ngram-1024", 17)
                if cancelled:
                    raise asyncio.CancelledError
            finally:
                await write_private_cost_event_for(
                    org="org", namespace=MIRROR, operation="worker.source", started=time.monotonic()
                )
    except asyncio.CancelledError:
        assert cancelled
    assert cost_accumulator.current() is outer
    assert outer.embed_tokens == 2
    assert events.get_emitter().stats() == before
    assert len(rows) == 1
    assert {
        key: rows[0][key]
        for key in (
            "org",
            "namespace",
            "agent",
            "user_id",
            "session_id",
            "operation",
            "embedding_tokens",
        )
    } == {
        "org": "org",
        "namespace": MIRROR,
        "agent": "-",
        "user_id": "-",
        "session_id": "-",
        "operation": "worker.source",
        "embedding_tokens": 17,
    }
    assert not diagnostics.codes_only()


async def test_failed_private_billing_insert_never_enters_global_emitter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import time

    from server.observability import events
    from server.workers.cost_event import write_private_cost_event_for

    async def insert(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private source details")

    monkeypatch.setattr(events, "get_client", lambda: SimpleNamespace(insert=insert))
    outer = cost_accumulator.begin_op()
    before = events.get_emitter().stats()
    with diagnostics.private_work(), cost_accumulator.isolated_op():
        cost_accumulator.report_embed("fake-ngram-1024", 17)
        await write_private_cost_event_for(
            org="org", namespace=MIRROR, operation="worker.source", started=time.monotonic()
        )
    assert cost_accumulator.current() is outer
    assert outer.embed_tokens == 0
    assert events.get_emitter().stats() == before
    assert not caplog.records


def test_configured_console_handler_drops_private_descendant_records(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    from server.core import logging_config

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(logging_config, "_configured", False)
    logging_config.configure_logging()
    logger = logging.getLogger("diagnostic-test.child")
    with diagnostics.private_work():
        logger.warning("private file content")
    logger.warning("ordinary record")
    captured = capsys.readouterr().out
    assert "private file content" not in captured
    assert "ordinary record" in captured


async def test_private_http_calls_emit_no_client_spans_or_http_metrics(spans: Any) -> None:
    import httpx
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    tracer, exporter = spans
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    HTTPXClientInstrumentor.instrument_client(client, meter_provider=meter_provider)
    try:
        with tracer.start_as_current_span("parent"):
            with diagnostics.private_work():
                assert (
                    await client.get("http://extractor/v1/extract?name=source.pdf")
                ).status_code == 200
        assert [s.name for s in exporter.get_finished_spans()] == ["parent"]
        assert reader.get_metrics_data() is None
        await client.get("http://extractor/healthz")
        assert reader.get_metrics_data() is not None
    finally:
        await client.aclose()
        meter_provider.shutdown()


def test_private_pricing_does_not_consume_an_ordinary_unpriced_model_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cost_accumulator, "_UNPRICED_SEEN", set())
    with diagnostics.private_work():
        assert cost_accumulator.embed_cost_usd("unpriced-private-model", 17) == 0
    assert cost_accumulator._UNPRICED_SEEN == set()
    cost_accumulator.embed_cost_usd("unpriced-private-model", 17)
    assert len([r for r in caplog.records if "no price for model" in r.message]) == 1


def test_private_sql_omits_application_capture_without_changing_database_policy(
    monkeypatch, tmp_path
):
    from server.core import ch

    captures = []
    monkeypatch.setattr(ch.settings, "ch_capture_statements", str(tmp_path / "statements.jsonl"))
    monkeypatch.setattr(ch.settings, "ch_log_comment_statement_digest", True)
    monkeypatch.setattr(ch, "capture_statement", lambda *args: captures.append(args))
    token = ch.query_tag_var.set("knowledge.private synthetic-address")
    try:
        public = ch.statement_settings("SELECT 'synthetic-address'")
        assert "synthetic-address" in public["log_comment"] and len(captures) == 1
        with diagnostics.private_work():
            private = ch.statement_settings(
                "SELECT 'synthetic-address'", {"log_queries": 1, "log_comment": "private-title"}
            )
            insert = ch.query_settings({"log_queries": 1, "log_comment": "private-title"})
        for actual in (private, insert):
            assert actual["log_queries"] == 1
            assert actual.get("log_comment", "") == ""
            assert set(actual).isdisjoint(
                {"log_query_views", "log_processors_profiles", "query_metric_log_interval"}
            )
        assert len(captures) == 1
        assert "synthetic-address" in ch.statement_settings("SELECT 1")["log_comment"]
    finally:
        ch.query_tag_var.reset(token)
