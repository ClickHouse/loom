"""Bounded caller metadata for actual Loom invocations, independent of authorization."""

from __future__ import annotations

import contextlib
import contextvars
import re
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

_ID = re.compile(r"[a-zA-Z0-9_.:-]{1,128}\Z")
_current: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "loom_prompt_context", default=None
)
_FIELDS = (
    "conversation_id",
    "prompt_id",
    "tool_call_id",
    "hook_event",
    "hook_name",
    "caller_type",
    "benchmark_run_id",
    "benchmark_case_id",
    "benchmark_variant",
)
_ORIGINS = {"tool", "hook", "worker", "unknown"}
_CALLERS = {"plugin", "sdk", "cli", "agent", "worker"}


def _identifier(value: Any) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else ""


def parse(value: Any) -> dict[str, str]:
    source = value if isinstance(value, Mapping) else {}
    parsed = {key: _identifier(source.get(key)) for key in _FIELDS}
    if parsed["caller_type"] not in _CALLERS:
        parsed.pop("caller_type")
    origin = source.get("origin")
    parsed["origin"] = origin if isinstance(origin, str) and origin in _ORIGINS else "unknown"
    return {key: item for key, item in parsed.items() if item}


def from_headers(headers: Mapping[str, str]) -> dict[str, str]:
    values = {key: headers.get("x-loom-" + key.replace("_", "-")) for key in _FIELDS}
    values["origin"] = headers.get("x-loom-call-origin")
    return parse(values)


@contextlib.contextmanager
def bind(value: Any, *, transport: str = "") -> Iterator[None]:
    values = parse(value)
    if transport:
        values["transport"] = transport
    token = _current.set(values)
    try:
        yield
    finally:
        _current.reset(token)


def stamp(span: Any) -> None:
    values = _current.get() or {}
    if values.get("conversation_id"):
        span.set_attribute("langfuse.session.id", values["conversation_id"])
    for key, value in values.items():
        if key.startswith("benchmark_"):
            attr = "loom.benchmark." + key.removeprefix("benchmark_")
        elif key in {"origin", "transport", "caller_type", "hook_event", "hook_name"}:
            attr = "loom.call." + key
        else:
            attr = "loom.prompt." + key
        span.set_attribute(attr, value)
        span.set_attribute("langfuse.trace.metadata.loom_" + key, value)


@contextlib.contextmanager
def mcp_call(
    params: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    operation: str = "tools/call",
) -> Iterator[Any]:
    meta = params.get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    values = from_headers(headers or {})
    supplied = meta.get("loom")
    if isinstance(supplied, Mapping):
        values.update(supplied)
    carrier = dict(headers or {})
    carrier.update(meta)
    with bind(values, transport="mcp"):
        with _call_span("mcp.tool", carrier, operation) as span:
            yield span


@contextlib.contextmanager
def http_call(headers: Mapping[str, str], operation: str) -> Iterator[Any]:
    with bind(from_headers(headers), transport="http"):
        with _call_span("loom.http", headers, operation) as span:
            yield span


@contextlib.contextmanager
def _call_span(name: str, carrier: Mapping[str, Any], operation: str) -> Iterator[Any]:
    from server.core.diagnostics import codes_only
    from server.observability import genai

    identity = genai._identity.set({})
    try:
        if codes_only():
            yield None
            return
        with _optional_span(name, carrier) as span:
            started = time.perf_counter()
            _attributes(span, {"loom.operation": operation, "langfuse.trace.name": operation})
            try:
                yield span
            except BaseException as error:
                status = getattr(error, "status_code", 0)
                result = (
                    "timeout"
                    if isinstance(error, TimeoutError) or status in (408, 504)
                    else ("refusal" if isinstance(status, int) and 400 <= status < 500 else "error")
                )
                _outcome(span, result)
                raise
            finally:
                _attributes(span, {"loom.call.duration_ms": (time.perf_counter() - started) * 1000})
    finally:
        genai._identity.reset(identity)


@contextlib.contextmanager
def _optional_span(name: str, meta: Mapping[str, Any]) -> Iterator[Any]:
    try:
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        carrier = {
            key: meta[key]
            for key in ("traceparent", "tracestate")
            if isinstance(meta.get(key), str) and len(meta[key]) <= 512
        }
        remote = TraceContextTextMapPropagator().extract(carrier)
        context = remote if trace.get_current_span(remote).get_span_context().is_valid else None
        manager = trace.get_tracer("loom.handlers").start_as_current_span(
            name,
            context=context,
            record_exception=False,
            set_status_on_exception=False,
            attributes={
                "loom.call.root": True,
                "loom.call.attempt_id": str(uuid.uuid4()),
                "langfuse.observation.type": "tool",
            },
        )
        span = manager.__enter__()
    except Exception:  # noqa: BLE001 — optional tracing must not fail a call
        yield None
        return
    try:
        try:
            if span is not None:
                stamp(span)
        except Exception:  # noqa: BLE001 — caller metadata must not fail a call
            pass
        yield span
    finally:
        try:
            manager.__exit__(*sys.exc_info())
        except Exception:  # noqa: BLE001 — closing telemetry must not fail a call
            pass


def _attributes(span: Any, values: dict[str, Any]) -> None:
    if span is None:
        return
    try:
        for key, value in values.items():
            span.set_attribute(key, value)
    except Exception:  # noqa: BLE001 — metadata must not change a response
        pass


def _outcome(span: Any, value: str) -> None:
    if span is None:
        return
    try:
        from opentelemetry.trace import StatusCode

        _attributes(
            span,
            {
                "loom.call.outcome": value,
                "loom.call.success": value == "success",
                "langfuse.observation.metadata.loom_outcome": value,
            },
        )
        if value != "success":
            span.set_status(StatusCode.ERROR)
    except Exception:  # noqa: BLE001 — outcome telemetry must not fail a call
        pass


def outcome(span: Any, response: Mapping[str, Any]) -> None:
    result = response.get("result")
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        detail = error.get("data")
        detail = detail if isinstance(detail, dict) else {}
        value = "refusal" if isinstance(code, int) and code in {-32600, -32601, -32602} else "error"
    elif isinstance(result, dict) and result.get("isError"):
        detail = result.get("structuredContent")
        detail = detail if isinstance(detail, dict) else {}
        value = (
            "refusal"
            if detail.get("category") in ("input", "auth", "capacity", "configuration")
            else "error"
        )
    else:
        detail, value = {}, "success"
    if detail.get("code") in ("BACKEND_TIMEOUT", "EMBED_TIMEOUT", "TIMEOUT") or detail.get(
        "status"
    ) in (408, 504):
        value = "timeout"
    _outcome(span, value)


def http_outcome(span: Any, status: int | None, request_id: str = "") -> None:
    status = 200 if status is None else status
    _attributes(span, {"loom.request_id": request_id} if request_id else {})
    _outcome(span, "success" if status < 400 else "refusal" if status < 500 else "error")
