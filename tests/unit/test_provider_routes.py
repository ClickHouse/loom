"""The key-entry endpoint is deployment-wide, not namespace-admin scope."""

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import AuthInfo
from server.insights.api.models_provider_setup import (
    ProviderSaveResult,
    ProviderSetupInput,
    ProviderValidation,
)
from server.insights.app import provider_activation as activation
from server.insights.app import provider_routes as routes
from server.insights.app import provider_runtime as runtime
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.operations.base import Role
from server.settings import settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(runtime, "_environment_settings", None)
    runtime.environment_settings()
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda _: False)
    monkeypatch.setattr(routes, "require_admin", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_existing_keys", AsyncMock(return_value={}))
    monkeypatch.setattr(routes, "configurations", AsyncMock(return_value=[]))
    monkeypatch.setattr(activation, "unpublishable_rebuild_tables", AsyncMock(return_value=()))
    monkeypatch.setattr(activation, "tables_with_vectors", AsyncMock(return_value=("chunks",)))
    monkeypatch.setattr(
        routes,
        "validate_providers",
        AsyncMock(return_value=ProviderValidation(ready=True, checks=[])),
    )
    app = FastAPI()
    app.include_router(routes.provider_router)
    return TestClient(app, base_url="http://localhost")


def payload() -> dict[str, object]:
    return {
        "llm": {"provider": "anthropic", "model": "claude-test"},
        "embedding": {"provider": "openai", "model": "text-embedding-test"},
        "keys": {"anthropic": "test-secret"},
    }


def test_local_operator_can_validate(client: TestClient) -> None:
    response = client.post("/v1/admin/models/validate", json=payload())
    assert response.status_code == 200
    assert response.json()["ready"]
    assert "test-secret" not in response.text


def test_reset_uses_a_required_string_revision(client, monkeypatch):
    handler = AsyncMock(return_value=ProviderSaveResult(config_id="test", state="active"))
    monkeypatch.setattr(routes, "reset_configuration", handler)
    response = client.post(
        "/v1/admin/models/reset", json={"expected_version": "1791195706575122049"}
    )
    assert response.status_code == 200
    assert handler.await_args.args[0].expected_version == "1791195706575122049"
    assert client.post("/v1/admin/models/reset", json={}).status_code == 422


def test_reset_refuses_cross_origin_requests(client, monkeypatch):
    handler = AsyncMock()
    monkeypatch.setattr(routes, "reset_configuration", handler)
    response = client.post(
        "/v1/admin/models/reset",
        json={"expected_version": "0"},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    handler.assert_not_awaited()


def test_retry_passes_only_the_requested_job_id(client, monkeypatch):
    config_id = uuid4()
    handler = AsyncMock(return_value=ProviderSaveResult(config_id=str(config_id), state="queued"))
    monkeypatch.setattr(routes, "retry_configuration", handler)
    response = client.post("/v1/admin/models/retry", json={"config_id": str(config_id)})
    assert response.status_code == 200
    assert handler.await_args.args[0].config_id == config_id
    assert client.post("/v1/admin/models/retry", json={"config_id": "invalid"}).status_code == 422


def test_retry_refuses_cross_origin_requests(client, monkeypatch):
    handler = AsyncMock()
    monkeypatch.setattr(routes, "retry_configuration", handler)
    response = client.post(
        "/v1/admin/models/retry",
        json={"config_id": str(uuid4())},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    handler.assert_not_awaited()


def test_status_exposes_only_key_presence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-private-value")
    response = client.get("/v1/admin/models")
    assert response.status_code == 200
    assert response.json()["configured_keys"]["openai"] is True
    assert response.json()["version"] == "0"
    assert "test-private-value" not in response.text


def test_status_allows_embedding_change_on_publishable_storage(client: TestClient) -> None:
    assert client.get("/v1/admin/models").json()["embedding_change_blocked"] == ""


def test_status_explains_why_this_storage_refuses_an_embedding_change(client, monkeypatch):
    monkeypatch.setattr(
        activation,
        "unpublishable_rebuild_tables",
        AsyncMock(return_value=(("memory_embeddings", "SharedReplacingMergeTree"),)),
    )
    blocked = client.get("/v1/admin/models").json()["embedding_change_blocked"]
    assert "loom.memory_embeddings uses SharedReplacingMergeTree" in blocked
    assert "server environment" in blocked
    monkeypatch.setattr(activation, "tables_with_vectors", AsyncMock(return_value=()))
    assert client.get("/v1/admin/models").json()["embedding_change_blocked"] == ""


def test_status_hides_private_rebuild_recipe_and_manifest(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "configurations",
        AsyncMock(
            return_value=[
                {
                    "config_id": uuid4(),
                    "state": "rebuilding",
                    "error_code": "",
                    "version": 1,
                    "progress": json.dumps(
                        {
                            "phase": "snapshot",
                            "rows_done": 10,
                            "recipe": {"fingerprint": "private"},
                            "manifest": {"tables": []},
                            "environment": "startup settings",
                        }
                    ),
                }
            ]
        ),
    )
    response = client.get("/v1/admin/models")
    assert response.status_code == 200
    assert response.json()["pending"]["progress"] == {
        "phase": "snapshot",
        "rows_done": 10,
        "table_count": len(REBUILD_TABLES),
    }


def test_status_uses_committed_models_before_replica_refresh(client, monkeypatch):
    saved = ProviderSetupInput.model_validate(payload())
    monkeypatch.setattr(routes, "open_configuration", Mock(return_value=saved))
    monkeypatch.setattr(
        routes,
        "configurations",
        AsyncMock(return_value=[{"state": "active", "version": 123, "envelope": "encrypted"}]),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "environment-embedding-key")
    monkeypatch.setattr(settings, "llm_model", "stale-local-model")
    monkeypatch.setattr(settings, "embedding_model", "stale-local-embedding")
    response = client.get("/v1/admin/models")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "123"
    assert body["source"] == "dashboard"
    assert body["llm_model"] == "claude-test"
    assert body["embedding_model"] == "text-embedding-test"
    assert body["configured_keys"]["anthropic"]
    assert not body["requires_setup"]
    assert "test-secret" not in response.text
    assert "environment-embedding-key" not in response.text


def test_reset_status_uses_environment_not_old_dashboard_keys(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "configurations",
        AsyncMock(return_value=[{"state": "environment", "version": 124}]),
    )
    monkeypatch.setattr(routes, "environment_provider_key", lambda _: "")
    monkeypatch.setattr(routes, "open_configuration", Mock())
    response = client.get("/v1/admin/models")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "124"
    assert body["source"] == "environment"
    assert not any(body["configured_keys"].values())
    assert body["requires_setup"]
    routes.open_configuration.assert_not_called()


@pytest.mark.parametrize("role", [Role.VIEWER, Role.WRITER, Role.ADMIN, Role.ORG_ADMIN])
def test_namespace_and_org_roles_cannot_manage_provider_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    auth = AuthInfo(
        subject="operator", org="org", namespaces=("*",), roles=(role,), is_service_account=False
    )
    monkeypatch.setattr(routes, "require_admin", AsyncMock(return_value=auth))
    assert client.post("/v1/admin/models/validate", json=payload()).status_code == 403
    assert client.post("/v1/admin/models/reset", json={"expected_version": "0"}).status_code == 403
    assert (
        client.post("/v1/admin/models/retry", json={"config_id": str(uuid4())}).status_code == 403
    )


def test_platform_admin_can_validate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = AuthInfo(
        subject="operator",
        org="org",
        namespaces=("*",),
        roles=(Role.PLATFORM_ADMIN,),
        is_service_account=False,
    )
    monkeypatch.setattr(routes, "require_admin", AsyncMock(return_value=auth))
    assert client.post("/v1/admin/models/validate", json=payload()).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Host": "attacker.example"},
    ],
)
def test_cross_site_and_rebinding_requests_are_rejected(
    client: TestClient, headers: dict[str, str]
) -> None:
    assert (
        client.post("/v1/admin/models/validate", json=payload(), headers=headers).status_code == 403
    )


def test_invalid_input_never_echoes_key(client: TestClient) -> None:
    body = payload()
    body["keys"] = {"anthropic": {"bad": "test-secret"}}
    response = client.post("/v1/admin/models/validate", json=body)
    assert response.status_code == 422
    assert "test-secret" not in response.text


def test_oversized_input_is_rejected(client: TestClient) -> None:
    body = payload()
    body["keys"] = {"anthropic": "x" * 32768}
    assert client.post("/v1/admin/models/validate", json=body).status_code == 413


def test_forms_cannot_trigger_billable_checks(client: TestClient) -> None:
    assert client.post("/v1/admin/models/validate", data={"key": "value"}).status_code == 415


def test_a_local_embedder_needs_only_the_llm_key(client, monkeypatch):
    local = payload()
    local["embedding"] = {"provider": "local", "model": "local", "dimensions": 384}
    local["keys"] = {"anthropic": "test-secret"}
    saved = ProviderSetupInput.model_validate(local)
    monkeypatch.setattr(routes, "open_configuration", Mock(return_value=saved))
    monkeypatch.setattr(
        routes,
        "configurations",
        AsyncMock(return_value=[{"state": "active", "version": 125, "envelope": "encrypted"}]),
    )
    monkeypatch.setattr(routes, "environment_provider_key", lambda _: "")
    body = client.get("/v1/admin/models").json()
    assert (body["embedding_provider"], body["embedding_model"]) == ("local", "local")
    assert not body["requires_setup"]
