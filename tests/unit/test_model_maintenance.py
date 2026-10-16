"""Maintenance rejects new API/MCP work while keeping operator recovery reachable."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.core.model_work_gate import ModelWorkGate
from server.transport.model_maintenance import ModelMaintenanceMiddleware


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/memory.set"),
        ("POST", "/v1/code.search"),
        ("POST", "/mcp"),
        ("POST", "/v1/knowledge.add"),
        ("GET", "/v1/task/watch"),
        ("GET", "/v1/knowledge.download"),
        ("GET", "/mcp"),
    ],
)
async def test_closed_gate_rejects_data_plane_without_running_handler(method, path):
    gate = ModelWorkGate()
    gate.close("change")
    app, send = AsyncMock(), AsyncMock()
    middleware = ModelMaintenanceMiddleware(app, gate)
    await middleware({"type": "http", "method": method, "path": path}, AsyncMock(), send)
    app.assert_not_awaited()
    response = send.await_args_list[0].args[0]
    assert response["status"] == 503
    assert (b"retry-after", b"3") in response["headers"]
    assert b"MODEL_CHANGE_IN_PROGRESS" in send.await_args_list[1].args[0]["body"]
    assert gate.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/"),
        ("GET", "/memories"),
        ("GET", "/admin/review_queue"),
        ("GET", "/assets/app.js"),
        ("GET", "/favicon.svg"),
        ("GET", "/v1/health"),
        ("GET", "/v1/whoami"),
        ("POST", "/v1/auth/login"),
        ("GET", "/v1/admin/models"),
        ("POST", "/v1/admin/models/reset"),
    ],
)
async def test_operator_can_inspect_and_recover_during_maintenance(method, path):
    gate = ModelWorkGate()
    gate.close("change")
    app = AsyncMock()
    await ModelMaintenanceMiddleware(app, gate)(
        {"type": "http", "method": method, "path": path}, AsyncMock(), AsyncMock()
    )
    app.assert_awaited_once()
    assert gate.active == 0


@pytest.mark.asyncio
async def test_streaming_response_is_not_drained_when_only_headers_have_been_sent():
    gate = ModelWorkGate()
    headers_sent, finish = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        headers_sent.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"done"})

    task = asyncio.create_task(
        ModelMaintenanceMiddleware(app, gate)(
            {"type": "http", "method": "POST", "path": "/mcp"}, AsyncMock(), AsyncMock()
        )
    )
    await headers_sent.wait()
    gate.close("change")
    assert not await gate.wait_drained("change", timeout_s=0)
    finish.set()
    await task
    assert await gate.wait_drained("change", timeout_s=0)


@pytest.mark.asyncio
async def test_closed_websocket_is_retryable_without_entering_handler():
    gate = ModelWorkGate()
    gate.close("change")
    app, send = AsyncMock(), AsyncMock()
    await ModelMaintenanceMiddleware(app, gate)(
        {"type": "websocket", "path": "/mcp"}, AsyncMock(), send
    )
    app.assert_not_awaited()
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1013})
