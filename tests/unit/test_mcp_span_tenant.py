"""An MCP call's span identity and capture consent name the tenant the gate
authorized, as HTTP's do.

A code.* / tracker.* input leaves `org` / `namespace` empty and is
authorized (and handled) under the deployment's plane. A span identity
built from the input alone would resolve the per-namespace capture consent
for prompt/completion text for ("-", "-") — the operator default — instead
of the plane namespace's own setting, and the spans would carry no tenant.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.transport import mcp_call, mcp_outcome


def test_a_code_call_resolves_consent_and_spans_under_the_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = OPERATIONS_BY_NAME["code.repos"]

    async def _handler(inp: Any) -> Any:
        return base.output_model.model_construct()

    op = dataclasses.replace(base, handler=_handler)
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, op.mcp_tool_name, op)
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-acme")

    async def _none(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(mcp_call, "_caller_defaults", _none)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)
    consent: list[tuple[str, str]] = []

    async def _resolve(org: str, namespace: str) -> str:
        consent.append((org, namespace))
        return "off"

    spans: list[dict[str, Any]] = []
    monkeypatch.setattr("server.observability.capture_consent.resolve", _resolve)
    monkeypatch.setattr(
        "server.observability.otel.set_loom_attributes", lambda **kw: spans.append(kw)
    )
    out = asyncio.run(mcp_call._call_tool(1, {"name": op.mcp_tool_name, "arguments": {}}))
    assert out["result"]["isError"] is False, out
    assert consent == [("acme", "code-acme")]
    assert spans and all((s["org"], s["namespace"]) == ("acme", "code-acme") for s in spans)


def test_a_batch_entry_span_carries_none_of_the_previous_entrys_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON-RPC batch shares one task, and the Langfuse span processor stamps
    the published identity on every span it starts. Entry 2's span, and anything
    it opens before publishing its own identity, must carry none of entry 1's."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from server.observability import genai, otel_langfuse
    from server.transport import mcp

    monkeypatch.setattr(otel_langfuse.settings, "langfuse_sample_rate", 1.0)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        otel_langfuse._langfuse_processor_class()(
            SimpleSpanProcessor(exporter), otel_langfuse._LangfuseSpanFilter()
        )
    )
    monkeypatch.setattr(trace, "get_tracer", lambda name, *a, **k: provider.get_tracer(name))

    async def _dispatch(req: Any) -> dict[str, Any]:
        if req["id"] == 1:  # entry 1 publishes its tenant, as a tools/call does
            genai.set_identity(org="acme", namespace="ns-a", user_id="u-a", session_id="s-a")
        return {"jsonrpc": "2.0", "id": req["id"], "result": {}}

    batch = [{"id": 1, "method": "tools/call"}, {"id": 2, "method": "resources/read"}]
    asyncio.run(mcp._dispatch_batch(batch, _dispatch))
    second = next(
        sp for sp in exporter.get_finished_spans() if sp.attributes.get("batch_index") == 1
    )
    leaked = {k: v for k, v in second.attributes.items() if k.startswith(("loom.", "langfuse."))}
    assert leaked == {}, leaked
