"""Live document telemetry retains request accounting without the payload."""

from unittest.mock import AsyncMock, Mock

import pytest

from server.core.operation_privacy import KNOWLEDGE_SOURCE_LIVE
from server.observability import ledger


@pytest.mark.parametrize("success", [True, False])
def test_live_ledger_ignores_arguments_results_and_exception_text(monkeypatch, success) -> None:
    dispatch = Mock(return_value=False)
    project = Mock(side_effect=AssertionError("a live payload reached the diagnostic projector"))
    monkeypatch.setattr(ledger, "_dispatch_event", dispatch)
    monkeypatch.setattr("server.observability.event_args.project_args", project)
    monkeypatch.setattr("server.observability.event_args.project_result", project)
    monkeypatch.setattr("server.observability.event_args.bounded_error", project)
    ledger.emit_agent_event(
        op_name=KNOWLEDGE_SOURCE_LIVE,
        identity={
            "org": "test-org",
            "namespace": "docs",
            "agent": "-",
            "user_id": "-",
            "session_id": "-",
        },
        request_id="req-1",
        latency_ms=22,
        success=success,
        error_code="" if success else "INTERNAL_ERROR",
        raw_args={"query": "input-canary", "run_id": "unknown-canary"},
        result={"document": {"text": "result-canary"}} if success else None,
        error=None if success else ValueError("exception-canary"),
    )
    dispatch.assert_called_once()
    event = dispatch.call_args.args[0]
    assert event.request_id == "req-1"
    assert event.operation == KNOWLEDGE_SOURCE_LIVE
    assert event.success is success
    assert event.latency_ms == 22
    assert not event.run_id
    assert not event.response_tokens
    assert "canary" not in str(event.args)


@pytest.mark.asyncio
async def test_live_mcp_capture_ignores_namespace_consent(monkeypatch) -> None:
    from server.observability import capture_consent
    from server.transport.mcp_call import _capture_level

    consent = AsyncMock(return_value="full")
    monkeypatch.setattr(capture_consent, "resolve", consent)
    identity = {"org": "test-org", "namespace": "docs"}
    assert await _capture_level(KNOWLEDGE_SOURCE_LIVE, identity) == "off"
    consent.assert_not_awaited()
    assert await _capture_level("knowledge.search", identity) == "full"
    consent.assert_awaited_once_with("test-org", "docs")


@pytest.mark.asyncio
async def test_registered_live_operation_is_read_only_and_disabled_without_a_directory() -> None:
    from server.knowledge.api.models_source_live import KnowledgeSourceLiveInput
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.http import build_router

    operation = OPERATIONS_BY_NAME[KNOWLEDGE_SOURCE_LIVE]
    assert operation.read_only
    assert operation.http_path in {route.path for route in build_router().routes}
    response = await operation.handler(
        KnowledgeSourceLiveInput(
            org="test-org", namespace="docs", source="test", resource_id="mcp://example/test"
        )
    )
    assert response.status == "unavailable"
    assert response.document is None
