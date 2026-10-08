"""A POST /v1/{op} body that is JSON but not an object is the op's 422, not a 500.

Every op's input is an object, so its model refuses a list, string or number
body. The ledger row and the identity readers read the body as a dict: such
a body reaching them would fail with AttributeError, so the client would see
an INTERNAL_ERROR for its own malformed request and no ledger row would land.
(test_op_transport_contract pins the 422 with the ledger stubbed out; this
test runs the real row writer's inputs.)
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.settings import settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", False)
    import server.transport.http as http

    rows: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        http, "emit_agent_event", lambda **kw: rows.append((kw["error_code"], kw["identity"]))
    )
    app = FastAPI()
    http.attach(app)
    c = TestClient(app, raise_server_exceptions=False)
    c.rows = rows  # type: ignore[attr-defined]
    return c


@pytest.mark.parametrize("body", [[1, 2], "text", 5, True])
def test_non_object_json_body_is_the_ops_422(client: TestClient, body: object) -> None:
    r = client.post("/v1/memory.get", json=body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "VALIDATION_FAILED"
    assert detail["errors"] and detail["errors"][0]["type"] == "model_type"
    [(code, identity)] = client.rows  # type: ignore[attr-defined]
    assert code == "VALIDATION_FAILED" and identity["namespace"] == "-"


def test_body_that_is_not_json_is_still_a_400(client: TestClient) -> None:
    r = client.post(
        "/v1/memory.get", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["detail"]["message"] == "Body is not valid JSON"
