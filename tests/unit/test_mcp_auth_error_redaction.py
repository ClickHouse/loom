"""MCP authentication refusals expose guidance without exception contents."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.transport import mcp


@pytest.mark.parametrize("method", ["post", "delete"])
@pytest.mark.parametrize("detail", ["/private/auth/keys.py:123", "Traceback: credential=secret"])
def test_permission_refusals_keep_exception_details_out_of_responses(
    monkeypatch: pytest.MonkeyPatch, method: str, detail: str
) -> None:
    async def refuse(_request: Any) -> None:
        raise PermissionError(detail)

    monkeypatch.setattr(mcp, "_verify_bearer", refuse)
    app = FastAPI()
    mcp.attach(app)
    with TestClient(app) as client:
        response = client.request(
            method, "/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "ping"}
        )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == (
        "an end-user token calls the HTTP API (/v1/<op>), not MCP"
    )
    assert detail not in response.text
    assert "www-authenticate" not in response.headers
    assert response.json()["id"] == (7 if method == "post" else "loom-transport")
