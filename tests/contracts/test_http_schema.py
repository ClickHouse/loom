"""Generate real HTTP transport requests with synthetic backing-store seams."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
import schemathesis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as generated
from hypothesis import strategies as st

from server.auth.jwt import mint_token
from server.operations.base import Role
from server.settings import settings

_PATH = "/v1/blacklist.list"
_ORG, _NS = "contract-a", "shared"


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    from server.auth import authz
    from server.memory.store import blacklist
    from server.operations.registry import ALL_OPERATIONS
    from server.transport import http

    calls: list[tuple[str, str]] = []

    async def no_grant(*args: Any, **kwargs: Any) -> None:
        return None

    async def rows(org: str, namespace: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append((org, namespace))
        return [
            {
                "pattern_id": "synthetic-pattern",
                "org": org,
                "namespace": namespace,
                "pattern": "synthetic rule for " + org + "/" + namespace,
                "pattern_type": "substring",
                "scope": "namespace",
                "reason": "Synthetic contract case",
                "created_by": "contract",
                "hit_count": 0,
                "created_at": datetime(2024, 1, 1, tzinfo=UTC),
            }
        ]

    selected = [op for op in ALL_OPERATIONS if op.name == "blacklist.list"]
    assert len(selected) == 1
    monkeypatch.setattr(http, "ALL_OPERATIONS", selected)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(authz, "_lookup_grant", no_grant)
    monkeypatch.setattr(blacklist, "list_for_scope", rows)
    app = FastAPI()
    http.attach(app)
    token = mint_token(
        subject="contract-reader",
        org=_ORG,
        namespaces=[_NS],
        roles=[Role.VIEWER.name.lower()],
        ttl_seconds=300,
        is_service_account=True,
    )
    document = deepcopy(app.openapi())
    request = document["paths"][_PATH]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    assert request["type"] == "object"
    request["additionalProperties"] = False
    for field in request["properties"].values():
        if field.get("type") == "string":
            field["maxLength"] = min(field.get("maxLength", 32), 32)
            assert field.get("minLength", 0) <= field["maxLength"]
    schema = schemathesis.openapi.from_dict(document)
    strategy = schema[_PATH]["POST"].as_strategy()
    with TestClient(app) as client:
        yield strategy, client, calls, {"Authorization": "Bearer " + token}


# The fixture holds only a read-only synthetic store. Each generated request clears
# its observation list; no generated example depends on a previous request.
_BOUNDED = generated(
    max_examples=24,
    derandomize=True,
    database=None,
    deadline=1000,
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@_BOUNDED
@given(data=st.data())
def test_generated_payloads_obey_the_actual_success_schema(transport, data) -> None:
    strategy, client, calls, headers = transport
    case = data.draw(strategy)
    case.body.update(org=_ORG, namespace=_NS)
    calls.clear()
    response = client.post(_PATH, json=case.body, headers=headers)
    assert response.status_code == 200, response.text
    case.validate_response(response)
    assert calls == [(_ORG, _NS)]
    assert response.json()["rows"][0]["pattern"] == "synthetic rule for contract-a/shared"


@_BOUNDED
@given(data=st.data(), foreign=st.sampled_from(["org", "namespace"]))
def test_generated_cross_scope_payloads_never_reach_the_store(transport, data, foreign) -> None:
    strategy, client, calls, headers = transport
    case = data.draw(strategy)
    case.body.update(org=_ORG, namespace=_NS)
    case.body[foreign] = "contract-b" if foreign == "org" else "private"
    calls.clear()
    response = client.post(_PATH, json=case.body, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
    assert calls == []
    assert "synthetic rule" not in response.text


@_BOUNDED
@given(data=st.data(), invalid=st.booleans())
def test_generated_unauthenticated_payloads_never_reach_the_store(transport, data, invalid) -> None:
    strategy, client, calls, _ = transport
    case = data.draw(strategy)
    case.body.update(org=_ORG, namespace=_NS)
    headers = {"Authorization": "Bearer invalid.synthetic.token"} if invalid else {}
    calls.clear()
    response = client.post(_PATH, json=case.body, headers=headers)
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "AUTH_INVALID"
    assert calls == []


@pytest.mark.parametrize("body", [[1], "text", True, {"limit": 0}, {"limit": 1001}, {"limit": []}])
def test_fixed_invalid_types_and_bounds_are_validation_errors(transport, body) -> None:
    _, client, calls, headers = transport
    if isinstance(body, dict):
        body = {"org": _ORG, "namespace": _NS, **body}
    response = client.post(_PATH, json=body, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"
    assert calls == []


def test_a_unicode_unknown_name_keeps_the_success_and_exact_json_feedback(transport) -> None:
    _, client, calls, headers = transport
    response = client.post(
        _PATH, json={"org": _ORG, "namespace": _NS, "\u2603": 1}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Loom-Ignored-Args"] == "%E2%98%83"
    assert response.json()["ignored_args"] == ["\u2603"]
    assert calls == [(_ORG, _NS)]
