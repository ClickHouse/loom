"""Explicit prompt correlation never changes stored identity or fills missing causality."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from server.observability import genai, prompt_context
from tests.unit.test_http_idempotency_after_auth import client as client
from tests.unit.test_mcp_idempotency_replay_conflict import gated as gated
from tests.unit.test_mcp_idempotency_replay_conflict import probe as probe


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    identity_token = genai._identity.set({})
    yield exporter
    genai._identity.reset(identity_token)
    provider.shutdown()


def metadata(prompt="prompt-a"):
    return {
        "conversation_id": "conversation",
        "prompt_id": prompt,
        "origin": "model",
        "tool_call_id": "call-a",
    }


def test_mcp_restores_explicit_parent_and_stamps_provider_children(spans):
    meta = {
        "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
        "loom": metadata(),
    }
    with prompt_context.mcp_call({"_meta": meta}) as call:
        genai.set_identity(
            org="org", namespace="ns", operation="memory.get", session_id="stored-session"
        )
        genai.stamp_identity(call)
        with trace.get_tracer("test").start_as_current_span("embed.provider") as child:
            genai.stamp_identity(child)
        prompt_context.outcome(call, {"result": {"isError": False}})
    child, root = spans.get_finished_spans()
    assert root.context.trace_id == int("1" * 32, 16)
    assert root.parent.span_id == int("2" * 16, 16)
    assert child.parent.span_id == root.context.span_id
    assert child.attributes["loom.prompt.prompt_id"] == "prompt-a"
    assert child.attributes["langfuse.session.id"] == "conversation"
    assert child.attributes["loom.org"] == "org"
    assert root.attributes["loom.call.root"] is True
    assert root.attributes["loom.call.success"] is True
    assert not any(key.startswith("gen_ai.usage") for key in root.attributes)


def test_errors_and_retries_have_one_span_per_attempt(spans):
    for failed in (True, False):
        with prompt_context.mcp_call({"_meta": {"loom": metadata()}}) as span:
            genai.set_identity(org="org", namespace="ns")
            genai.stamp_identity(span)
            prompt_context.outcome(span, {"result": {"isError": failed}})
    first, second = spans.get_finished_spans()
    assert first.context.span_id != second.context.span_id
    assert first.attributes["loom.call.success"] is False
    assert first.status.status_code is trace.StatusCode.ERROR
    assert second.attributes["loom.call.success"] is True
    assert (
        first.attributes["loom.prompt.tool_call_id"]
        == second.attributes["loom.prompt.tool_call_id"]
    )


@pytest.mark.asyncio
async def test_concurrent_turns_and_unattributed_call_do_not_share_context(spans):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first():
        with prompt_context.bind(metadata("a")):
            entered.set()
            await release.wait()
            with trace.get_tracer("test").start_as_current_span("a") as span:
                prompt_context.stamp(span)

    async def second():
        await entered.wait()
        with prompt_context.bind(metadata("b")):
            with trace.get_tracer("test").start_as_current_span("b") as span:
                prompt_context.stamp(span)
        release.set()

    await asyncio.gather(first(), second())
    with prompt_context.mcp_call({"_meta": {"traceparent": "malformed"}}):
        pass
    by_name = {span.name: span for span in spans.get_finished_spans()}
    assert by_name["a"].attributes["loom.prompt.prompt_id"] == "a"
    assert by_name["b"].attributes["loom.prompt.prompt_id"] == "b"
    assert "loom.prompt.prompt_id" not in by_name["mcp.tool"].attributes


def test_optional_ids_are_independent_and_reject_content():
    assert prompt_context.from_headers({"x-loom-prompt-id": "p"}) == {
        "origin": "unknown",
        "prompt_id": "p",
    }
    assert prompt_context.parse({"conversation_id": "s", "prompt_id": "line\nbreak"}) == {
        "conversation_id": "s",
        "origin": "unknown",
    }
    assert prompt_context.parse({"conversation_id": "s", "prompt_id": "x" * 129}) == {
        "conversation_id": "s",
        "origin": "unknown",
    }
    parsed = prompt_context.from_headers(
        {"x-loom-conversation-id": "s", "x-loom-prompt-id": "p", "x-loom-call-origin": "hook"}
    )
    assert parsed == {"conversation_id": "s", "prompt_id": "p", "origin": "hook"}


def test_broken_optional_tracer_does_not_prevent_the_call(monkeypatch):
    class BrokenTracer:
        @contextmanager
        def start_as_current_span(self, *args, **kwargs):
            raise RuntimeError("tracing unavailable")
            yield  # The SDK starts a span when entering its context manager.

    monkeypatch.setattr(trace, "get_tracer", lambda *args: BrokenTracer())
    reached = []
    with prompt_context.mcp_call({"_meta": {"loom": metadata()}}) as span:
        reached.append("handler")
        assert span is None
    assert reached == ["handler"]


@pytest.mark.parametrize("handler_fails", [False, True])
def test_broken_tracer_exit_preserves_the_handler_outcome(monkeypatch, handler_fails):
    class BrokenTracer:
        @contextmanager
        def start_as_current_span(self, *args, **kwargs):
            try:
                yield None
            finally:
                raise RuntimeError("tracing unavailable")

    monkeypatch.setattr(trace, "get_tracer", lambda *args: BrokenTracer())
    error = ValueError("handler failed")

    def call():
        with prompt_context.mcp_call({}):
            if handler_fails:
                raise error
            return "result"

    if handler_fails:
        with pytest.raises(ValueError) as raised:
            call()
        assert raised.value is error
    else:
        assert call() == "result"


@pytest.mark.asyncio
async def test_dispatch_error_has_prompt_context_and_does_not_leak_to_the_next_call(
    monkeypatch, spans
):
    from server.transport import mcp

    async def refuse(*args, **kwargs):
        genai.set_identity(org="org", namespace="ns", operation="memory.get")
        genai.stamp_identity(trace.get_current_span())
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid"}}

    monkeypatch.setattr(mcp, "_call_tool", refuse)
    result = await mcp._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "loom_memory_get", "_meta": {"loom": metadata()}},
        }
    )
    assert "error" in result
    root = spans.get_finished_spans()[0]
    assert root.attributes["loom.prompt.prompt_id"] == "prompt-a"
    assert root.attributes["loom.call.success"] is False
    with trace.get_tracer("test").start_as_current_span("after") as after:
        genai.stamp_identity(after)
    assert "loom.prompt.prompt_id" not in spans.get_finished_spans()[-1].attributes


@pytest.mark.asyncio
async def test_http_prompt_binding_resets_after_a_refusal(monkeypatch, spans):
    from fastapi import HTTPException, Request, Response

    from server.transport import http

    monkeypatch.setattr(http, "_begin_request", lambda *args: "request")

    def refuse(request):
        with trace.get_tracer("test").start_as_current_span("http-refusal") as span:
            prompt_context.stamp(span)
        raise HTTPException(status_code=401, detail="missing token")

    monkeypatch.setattr(http, "_extract_auth", refuse)
    endpoint = http._make_endpoint(None)
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-loom-conversation-id", b"conversation"),
                (b"x-loom-prompt-id", b"prompt-a"),
            ],
        }
    )
    with pytest.raises(HTTPException) as raised:
        await endpoint(request, Response())
    assert raised.value.status_code == 401
    assert spans.get_finished_spans()[0].attributes["loom.prompt.prompt_id"] == "prompt-a"
    with trace.get_tracer("test").start_as_current_span("after-http") as span:
        prompt_context.stamp(span)
    assert "loom.prompt.prompt_id" not in spans.get_finished_spans()[-1].attributes


def test_invocation_origin_and_benchmark_ids_do_not_require_prompt_ids():
    parsed = prompt_context.from_headers(
        {
            "x-loom-call-origin": "hook",
            "x-loom-hook-event": "SessionStart",
            "x-loom-benchmark-run-id": "run-7",
            "x-loom-benchmark-case-id": "case-2",
            "x-loom-benchmark-variant": "with-memory",
            "x-loom-caller-type": "plugin",
        }
    )
    assert parsed == {
        "origin": "hook",
        "hook_event": "SessionStart",
        "caller_type": "plugin",
        "benchmark_run_id": "run-7",
        "benchmark_case_id": "case-2",
        "benchmark_variant": "with-memory",
    }
    assert prompt_context.parse({"origin": "guess", "prompt_id": "p"}) == {
        "origin": "unknown",
        "prompt_id": "p",
    }
    assert prompt_context.parse({"origin": "tool", "benchmark_case_id": "private\ntext"}) == {
        "origin": "tool"
    }


def test_invocation_metadata_is_structured_and_does_not_copy_usage(spans):
    with prompt_context.mcp_call(
        {
            "_meta": {
                "loom": {
                    "origin": "tool",
                    "tool_call_id": "stable-1",
                    "benchmark_run_id": "bench-1",
                }
            }
        }
    ) as span:
        genai.set_identity(org="org", namespace="ns", operation="memory.get")
        genai.stamp_identity(span)
        prompt_context.outcome(span, {"result": {"isError": False}})
    root = spans.get_finished_spans()[0]
    assert root.attributes["loom.call.origin"] == "tool"
    assert root.attributes["langfuse.trace.metadata.loom_benchmark_run_id"] == "bench-1"
    assert root.attributes["loom.call.transport"] == "mcp"
    assert root.attributes["loom.call.outcome"] == "success"
    assert root.attributes["loom.call.duration_ms"] >= 0
    assert not any(k.startswith("gen_ai.usage") for k in root.attributes)
    assert not any("bench-1" in t for t in root.attributes["langfuse.trace.tags"])


@pytest.mark.asyncio
async def test_invocation_mcp_protocol_refusal_has_one_attempt_span(spans):
    from server.transport import mcp

    result = await mcp._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}
    )
    assert "error" in result
    roots = [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    assert len(roots) == 1
    assert roots[0].attributes["loom.call.outcome"] == "refusal"
    assert roots[0].attributes["loom.call.origin"] == "unknown"


@pytest.mark.asyncio
async def test_invocation_http_early_refusal_has_operation_and_outcome(monkeypatch, spans):
    from types import SimpleNamespace

    from fastapi import HTTPException, Request, Response

    from server.transport import http

    monkeypatch.setattr(http, "_begin_request", lambda *args: "request-safe")

    def refuse(request):
        raise HTTPException(status_code=401, detail="must-not-export-this-secret")

    monkeypatch.setattr(http, "_extract_auth", refuse)
    endpoint = http._make_endpoint(SimpleNamespace(name="memory.get"))
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-loom-call-origin", b"hook"), (b"x-loom-hook-event", b"SessionStart")],
        }
    )
    with pytest.raises(HTTPException):
        await endpoint(request, Response())
    roots = [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    assert len(roots) == 1
    attrs = roots[0].attributes
    assert attrs["loom.operation"] == "memory.get"
    assert attrs["loom.call.outcome"] == "refusal"
    assert attrs["loom.call.origin"] == "hook"
    assert "must-not-export-this-secret" not in str(attrs)


@pytest.mark.parametrize("origin", [None, [], {}, 3, "automatic-guess"])
def test_invocation_invalid_origin_remains_unknown(origin):
    assert prompt_context.parse({"origin": origin}) == {"origin": "unknown"}


@pytest.mark.parametrize(
    "response, expected",
    [
        (
            {
                "result": {
                    "isError": True,
                    "structuredContent": {"category": "auth", "code": "AUTH_INSUFFICIENT"},
                }
            },
            "refusal",
        ),
        (
            {"error": {"code": -32603, "data": {"code": "BACKEND_TIMEOUT", "status": 504}}},
            "timeout",
        ),
        ({"error": {"code": -32603, "message": "private failure"}}, "error"),
    ],
)
def test_invocation_outcomes_export_no_error_text(spans, response, expected):
    with prompt_context.mcp_call({}) as span:
        prompt_context.outcome(span, response)
    root = spans.get_finished_spans()[0]
    assert root.attributes["loom.call.outcome"] == expected
    assert "private failure" not in str(root.attributes)
    assert not root.events


def test_invocation_retry_identity_is_distinct_from_received_attempts(spans):
    for _unused in range(2):
        with prompt_context.mcp_call(
            {"_meta": {"loom": {"origin": "tool", "tool_call_id": "stable"}}}
        ) as span:
            prompt_context.outcome(span, {"result": {}})
    first, second = spans.get_finished_spans()
    assert first.attributes["loom.call.attempt_id"] != second.attributes["loom.call.attempt_id"]
    assert (
        first.attributes["loom.prompt.tool_call_id"]
        == second.attributes["loom.prompt.tool_call_id"]
        == "stable"
    )


def test_invocation_exception_is_safe_and_identity_restores(spans):
    genai.set_identity(org="outer", namespace="outside")
    with pytest.raises(TimeoutError):
        with prompt_context.mcp_call({}):
            assert not genai._identity.get()
            raise TimeoutError("private SQL or credential")
    root = spans.get_finished_spans()[0]
    assert root.attributes["loom.call.outcome"] == "timeout"
    assert root.attributes["loom.call.origin"] == "unknown"
    assert "loom.org" not in root.attributes
    assert not root.events
    assert genai._identity.get()["org"] == "outer"


def test_invocation_private_export_boundary_still_drops_call_metadata(monkeypatch):
    from server.core.diagnostics import private_work
    from server.observability import otel_langfuse

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    monkeypatch.setattr(otel_langfuse.settings, "langfuse_sample_rate", 1.0)
    provider.add_span_processor(
        otel_langfuse._langfuse_processor_class()(
            SimpleSpanProcessor(exporter), otel_langfuse._LangfuseSpanFilter()
        )
    )
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    with prompt_context.mcp_call({"_meta": {"loom": {"origin": "tool"}}}) as span:
        with private_work():
            with trace.get_tracer("test").start_as_current_span("private.child"):
                pass
            span.end()
    assert exporter.get_finished_spans() == ()
    provider.shutdown()


def test_invocation_catalog_and_unknown_tool_each_have_one_received_attempt(monkeypatch, spans):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.settings import settings
    from server.transport import mcp

    monkeypatch.setattr(settings, "identity_provider", "single-user")
    app = FastAPI()
    mcp.attach(app)
    with TestClient(app) as client:
        catalog = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "loom_op",
                    "arguments": {"op": "help"},
                    "_meta": {"loom": {"origin": "tool", "benchmark_case_id": "catalog"}},
                },
            },
        )
        unknown = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "not-a-real-tool", "arguments": {}},
            },
        )
    assert catalog.status_code == unknown.status_code == 200
    assert not catalog.json()["result"]["isError"]
    assert unknown.json()["result"]["isError"]
    roots = [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    assert len(roots) == 2
    assert roots[0].attributes["loom.operation"] == "loom_op.help"
    assert roots[0].attributes["loom.benchmark.case_id"] == "catalog"
    assert roots[0].attributes["loom.call.outcome"] == "success"
    assert roots[1].attributes["loom.call.outcome"] == "refusal"


def test_invocation_worker_origin_is_scoped_without_counting_a_received_call(spans):
    from server.observability import otel

    with otel.worker_pass_span("example") as root:
        with trace.get_tracer("test").start_as_current_span("worker.child") as child:
            genai.stamp_identity(child)
    child, root = spans.get_finished_spans()
    assert child.attributes["loom.call.origin"] == "worker"
    assert child.attributes["loom.call.caller_type"] == "worker"
    assert not root.attributes.get("loom.call.root")
    with trace.get_tracer("test").start_as_current_span("outside") as span:
        prompt_context.stamp(span)
    assert "loom.call.origin" not in spans.get_finished_spans()[-1].attributes


def test_invocation_http_default_response_status_is_success(spans):
    with prompt_context.http_call({}, "memory.get") as span:
        prompt_context.http_outcome(span, None, "request-example")
    root = spans.get_finished_spans()[0]
    assert root.attributes["loom.call.outcome"] == "success"
    assert root.attributes["loom.request_id"] == "request-example"


@pytest.mark.parametrize("target", ["memory.set", "memory_set", " memory.set "])
def test_review_operation_refusal_retains_normalized_target(monkeypatch, spans, target):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.settings import settings
    from server.transport import mcp

    monkeypatch.setattr(settings, "identity_provider", "single-user")
    app = FastAPI()
    mcp.attach(app)
    with TestClient(app) as client:
        response = client.post(
            "/mcp?toolset=readonly",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "loom_op", "arguments": {"op": target}},
            },
        )
    assert response.json()["result"]["isError"] is True
    roots = [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    assert len(roots) == 1
    assert roots[0].attributes["loom.operation"] == "memory.set"
    assert roots[0].attributes["loom.call.outcome"] == "refusal"


@pytest.mark.asyncio
async def test_review_operation_activation_retains_operation(monkeypatch, spans):
    from server.transport import mcp, mcp_call

    async def primer(*args):
        return "primer"

    monkeypatch.setattr(mcp_call, "_render_primer_and_emit", primer)
    response = await mcp._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "loom_activate"}}
    )
    assert response["result"]["isError"] is False
    roots = [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    assert len(roots) == 1
    assert roots[0].attributes["loom.operation"] == "loom_activate"
    assert roots[0].attributes["loom.call.outcome"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
async def test_review_protocol_version_refusal(monkeypatch, spans, version):
    from server.transport import mcp
    from server.transport.mcp_config import META_PROTOCOL_VERSION, UNSUPPORTED_PROTOCOL_VERSION

    response = await mcp._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "not-a-real-tool",
                "_meta": {META_PROTOCOL_VERSION: version},
            },
        }
    )
    if version == "2026-07-28":
        assert response["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
    else:
        assert response["result"]["isError"] is True
    roots = [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    assert len(roots) == 1
    assert roots[0].attributes["loom.call.outcome"] == "refusal"


@pytest.mark.parametrize(
    "arguments, operation, failed",
    [
        ({"op": "help"}, "loom_op.help", False),
        ({"op": " Catalog "}, "loom_op.help", False),
        (None, "loom_op.help", False),
        ({"op": "definitely_missing"}, "loom_op", True),
        ({"op": 23}, "loom_op", True),
        ("invalid", "loom_op", True),
    ],
)
def test_review_operation_label_distinguishes_help_from_invalid_targets(
    monkeypatch, spans, arguments, operation, failed
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.settings import settings
    from server.transport import mcp

    monkeypatch.setattr(settings, "identity_provider", "single-user")
    app = FastAPI()
    mcp.attach(app)
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "loom_op", "arguments": arguments},
            },
        )
    body = response.json()
    assert bool(body.get("error") or body.get("result", {}).get("isError")) is failed
    roots = [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    assert len(roots) == 1
    assert roots[0].attributes["loom.operation"] == operation
    assert roots[0].attributes["loom.call.outcome"] == ("refusal" if failed else "success")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, expected", [("embed", "timeout"), ("http", "timeout"), ("busy", "error")]
)
async def test_review_http_timeout_conversion_preserves_classification(
    monkeypatch, spans, kind, expected
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from fastapi import HTTPException

    from server.core.errors import EmbedTimeoutError
    from server.transport import http

    code = "UPSTREAM_UNAVAILABLE" if kind == "busy" else "EMBED_TIMEOUT"

    async def handler(payload):
        if kind == "embed":
            raise EmbedTimeoutError("private provider failure")
        raise HTTPException(503, detail={"code": code, "message": "private provider failure"})

    monkeypatch.setattr(http.gates, "tenant_slot", lambda *args: nullcontext())
    monkeypatch.setattr(http.llm_free, "serving", lambda *args: nullcontext())
    monkeypatch.setattr(http, "_emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(http, "_record_failure", lambda *args, **kwargs: None)
    op = SimpleNamespace(name="memory.search", handler=handler, idempotent=True)
    call = SimpleNamespace(
        request_id="request", raw_body={}, identity={"namespace": "ns"}, auth=None, target="ns"
    )
    with pytest.raises(HTTPException) as raised:
        with prompt_context.http_call({}, op.name):
            await http._run_handler(op, None, call)
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == code
    root = spans.get_finished_spans()[0]
    assert root.attributes["loom.call.outcome"] == expected
    assert "private provider failure" not in str(root.attributes)
    assert not root.events


def _assert_authenticated_replay_roots(roots):
    assert len(roots) == 2
    for root in roots:
        assert root.attributes["loom.org"] == "o"
        assert root.attributes["loom.namespace"] == "n"
        assert root.attributes["langfuse.user.id"] == "alice"
        assert {"org:o", "namespace:n"} <= set(root.attributes["langfuse.trace.tags"])
        assert root.attributes["loom.request_id"]
        assert root.attributes["loom.prompt.tool_call_id"] == "logical-replay"
        assert root.attributes["loom.call.outcome"] == "success"
    assert (
        roots[0].attributes["loom.call.attempt_id"] != roots[1].attributes["loom.call.attempt_id"]
    )
    assert roots[0].attributes["loom.request_id"] != roots[1].attributes["loom.request_id"]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_review_authenticated_early_refusal_uses_token_identity(
    monkeypatch, client, spans, transport
):
    from server.auth import bearer, jwt
    from server.transport import mcp

    http_client, rows, ran = client
    monkeypatch.setattr(jwt, "authenticate_token", bearer.authenticate_token)
    headers = {"Authorization": "Bearer alice", "X-Loom-Tool-Call-Id": "logical-refusal"}
    if transport == "http":
        result = http_client.post(
            "/v1/memory.unlock", json={"org": "forged", "namespace": "forged"}, headers=headers
        )
        assert result.status_code == 422
    else:
        mcp.attach(http_client.app)
        result = http_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "does_not_exist",
                    "arguments": {"org": "forged", "namespace": "forged"},
                },
            },
        )
        assert result.json().get("error") or result.json().get("result", {}).get("isError")
    assert not rows and not ran
    roots = [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    assert len(roots) == 1
    attributes = roots[0].attributes
    assert attributes["loom.org"] == "o"
    assert attributes["langfuse.user.id"] == "alice"
    assert "org:o" in attributes["langfuse.trace.tags"]
    assert attributes["loom.namespace"] == "-"
    assert "forged" not in str(attributes)
    assert attributes["loom.prompt.tool_call_id"] == "logical-refusal"
    assert attributes["loom.call.outcome"] == "refusal"


def test_review_authenticated_http_replay_retains_identity(client, spans):
    from tests.unit.test_http_idempotency_after_auth import BODY

    http_client, rows, ran = client
    headers = {
        "Authorization": "Bearer alice",
        "Idempotency-Key": "identity-replay",
        "X-Loom-Call-Origin": "tool",
        "X-Loom-Tool-Call-Id": "logical-replay",
    }
    first = http_client.post("/v1/memory.unlock", json=BODY, headers=headers)
    replay = http_client.post("/v1/memory.unlock", json=BODY, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert ran == ["k"] and len(rows) == 1
    _assert_authenticated_replay_roots(
        [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    )


@pytest.mark.asyncio
async def test_review_authenticated_mcp_replay_retains_identity(monkeypatch, gated, spans):
    from dataclasses import replace

    from pydantic import BaseModel

    from server.transport import mcp, mcp_call
    from tests.unit.test_mcp_idempotency_replay_conflict import TOOL, _principal

    class ScopedInput(BaseModel):
        text: str
        org: str
        namespace: str
        agent: str

    op = replace(mcp_call.TOOLS_BY_NAME[TOOL], input_model=ScopedInput)
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, TOOL, op)
    results = []
    for req_id in (1, 2):
        results.append(
            await mcp._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {
                        "name": TOOL,
                        "arguments": {
                            "text": "safe fixture",
                            "org": "o",
                            "namespace": "n",
                            "agent": "a",
                            "_idempotency_key": "identity-replay",
                        },
                        "_meta": {"loom": {"origin": "tool", "tool_call_id": "logical-replay"}},
                    },
                },
                auth=_principal("alice"),
            )
        )
    assert all(not r["result"]["isError"] for r in results)
    assert results[1]["result"]["_meta"] == {"idempotentReplay": True}
    store, ran = gated
    assert ran == ["safe fixture"] and len(store.rows) == 1
    _assert_authenticated_replay_roots(
        [s for s in spans.get_finished_spans() if s.attributes.get("loom.call.root")]
    )
