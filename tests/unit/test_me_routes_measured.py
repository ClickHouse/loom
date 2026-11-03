"""A /v1/me route is an operation.

The Setup page's routes (`/v1/me/service_accounts`, the
mint, the default-namespace save) are ranked with every other operation by
their rows, so the transport's middleware gives them the request id, the query tag, the work bag, both timing headers
and one `me.<route>` ledger row carrying the verified caller.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from server.observability import ledger
from server.transport import http as http_transport


def test_operation_names_follow_the_path() -> None:
    assert http_transport.me_operation_name("/v1/me/sharing_summary") == "me.sharing_summary"
    assert (
        http_transport.me_operation_name("/v1/me/service_accounts.create")
        == "me.service_accounts.create"
    )
    assert http_transport.me_operation_name("/v1/me/setup/mcp_config") == "me.setup.mcp_config"
    assert http_transport.me_operation_name("/v1/me/") == "me"


@pytest.fixture
def app_and_events(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, list]:
    captured: list = []
    monkeypatch.setattr(ledger, "get_emitter", lambda: SimpleNamespace(emit=captured.append))
    app = FastAPI()

    @app.get("/v1/me/ping")
    async def ping(request: Request) -> dict[str, str]:
        request.state.loom_caller = ("sa:claude-abc", "acme")
        return {"ok": "yes"}

    @app.get("/v1/me/boom")
    async def boom() -> dict[str, str]:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID"})

    @app.get("/v1/other")
    async def other() -> dict[str, str]:
        return {"ok": "yes"}

    http_transport.attach(app)
    return app, captured


def test_a_me_route_answers_with_timing_and_one_ledger_row(
    app_and_events: tuple[FastAPI, list],
) -> None:
    app, captured = app_and_events
    client = TestClient(app)
    r = client.get("/v1/me/ping", headers={"X-Request-Id": "rid-1"})
    assert r.status_code == 200
    assert r.headers["X-Request-Id"] == "rid-1"
    assert r.headers["Server-Timing"].startswith("app;dur=")
    (ev,) = [e for e in captured if e.operation.startswith("me.")]
    assert ev.operation == "me.ping" and ev.request_id == "rid-1" and ev.success is True
    assert ev.org == "acme" and ev.user_id == "sa:claude-abc" and ev.subject == "sa:claude-abc"
    assert ev.namespace == "-" and ev.agent == "-" and ev.kind == "request"


def test_a_refused_me_route_is_a_failed_row_and_other_paths_pass_through(
    app_and_events: tuple[FastAPI, list],
) -> None:
    app, captured = app_and_events
    client = TestClient(app)
    r = client.get("/v1/me/boom")
    assert r.status_code == 401 and "Server-Timing" in r.headers
    (ev,) = [e for e in captured if e.operation == "me.boom"]
    assert ev.success is False and ev.error_code == "HTTP_401" and ev.org == "-"
    r2 = client.get("/v1/other")
    assert r2.status_code == 200 and "Server-Timing" not in r2.headers
    assert not [e for e in captured if e.operation == "me.other"]


def test_a_request_id_that_is_not_a_token_is_replaced_on_the_response_and_the_row(
    app_and_events: tuple[FastAPI, list],
) -> None:
    app, captured = app_and_events
    r = TestClient(app).get("/v1/me/ping", headers={"X-Request-Id": "two words"})
    made = r.headers["X-Request-Id"]
    assert made != "two words" and str(uuid.UUID(made)) == made
    (ev,) = [e for e in captured if e.operation.startswith("me.")]
    assert ev.request_id == made
