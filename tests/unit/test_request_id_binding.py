"""A request's id reaches its log lines and its ClickHouse queries on both
transports: the ledger row, the query_log row and a log line join on one id."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from server.core.ch import current_query_tag
from server.core.logging_config import request_id_var
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import http as http_transport
from server.transport._common import bind_request


def test_bind_request_sets_the_log_id_and_the_query_tag() -> None:
    async def _run() -> tuple[str | None, str]:
        bind_request("memory.get", "rid-1")
        return request_id_var.get(), current_query_tag()

    assert asyncio.run(_run()) == ("rid-1", "memory.get rid-1")


def test_an_mcp_tool_call_binds_its_request_id_for_log_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import mcp_call

    async def _no_defaults(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)
    op = OPERATIONS_BY_NAME["memory.get"]

    async def _run() -> tuple[str, str | None, str]:
        request_id, _ = await mcp_call._bind_caller_identity(op, {}, None, None)
        return request_id, request_id_var.get(), current_query_tag()

    request_id, logged, tag = asyncio.run(_run())
    assert logged == request_id
    assert tag == f"memory.get {request_id}"


def test_an_op_call_binds_the_replacement_id_to_its_query_tag() -> None:
    def begin() -> tuple[str, str, str]:
        scope = {"type": "http", "headers": [(b"x-request-id", b"x:y")], "path": "/v1/memory.get"}
        response = Response()
        rid = http_transport._begin_request(
            OPERATIONS_BY_NAME["memory.get"], Request(scope), response
        )
        return rid, response.headers["X-Request-Id"], current_query_tag()

    rid, echoed, tag = contextvars.copy_context().run(begin)
    assert rid == echoed and str(uuid.UUID(rid)) == rid
    assert tag == f"memory.get {rid}"
