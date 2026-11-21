"""Cell action models and admission refuse authority that cannot name a fresh bot."""

from __future__ import annotations

import importlib
from typing import Any

import httpx
import pytest

ACTION_PATHS = [
    "capabilities",
    "settings_list",
    "settings_set",
    "settings_history",
    "signing_keys_list",
    "signing_keys_rotate",
    "signing_keys_retire",
    "subject_revoke",
]


def _context() -> dict[str, Any]:
    return {
        "version": 1,
        "expected_cell": "alpha",
        "expected_deployment_id": "deploy-alpha",
        "expected_bot": "automation-alpha",
        "run_id": "a046002d-c888-4b54-852b-167f1cb8a443",
        "attempt": 1,
        "command_digest": "1" * 64,
    }


def test_typed_inputs_keep_the_raw_setting_and_exact_global_subject() -> None:
    models = importlib.import_module("server.insights.api.models_cell_admin")
    setting = models.CellSettingsSetInput.model_validate(
        {
            "context": _context(),
            "name": "memory_ask_recency_weight",
            "value": "0.25",
            "clear": False,
            "expected": None,
        }
    )
    subject = models.CellSubjectRevokeInput.model_validate(
        {"context": _context(), "subject": "sa:worker", "reason": "retire"}
    )
    assert setting.value == "0.25" and setting.clear is False
    assert subject.subject == "sa:worker" and subject.reason == "retire"


@pytest.mark.parametrize("action", ACTION_PATHS)
async def test_authoff_cannot_admit_cell_administration(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI

    from server.settings import settings

    routes = importlib.import_module("server.insights.app.cell_admin_routes")
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    app = FastAPI()
    app.include_router(routes.cell_admin_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://cell.example.com"
    ) as client:
        response = await client.post(f"/v1/admin/cell.{action}", json={})
    # Authority is decided before malformed body validation or store/business work.
    assert response.status_code == 403


@pytest.mark.parametrize(
    "fields,expected_status",
    [
        ({"service_account": False}, 403),
        ({"subject": "person@example.com"}, 403),
        ({"creator": "person@example.com"}, 403),
        ({"agent_principal": "agent:worker"}, 403),
        ({"ns_roles": (("work", "admin"),)}, 403),
        ({"namespaces": ("work",)}, 403),
        ({"impersonated_by": "person@example.com"}, 403),
        ({}, 422),
    ],
    ids=[
        "human",
        "subject",
        "delegated",
        "agent",
        "scoped-role",
        "namespace",
        "impersonated",
        "healthy",
    ],
)
async def test_verified_platform_role_does_not_replace_the_positive_bot_guard(
    fields: dict[str, Any],
    expected_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from server.insights.app.admin_routes import require_admin
    from server.settings import settings
    from tests.unit.principals import principal

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")

    routes = importlib.import_module("server.insights.app.cell_admin_routes")
    options = {
        "subject": "sa:automation-alpha",
        "namespaces": (),
        "token_id": "automation-token",
        **fields,
    }
    auth = principal("platform_admin", **options)

    async def verified_admin() -> Any:
        return auth

    app = FastAPI()
    app.include_router(routes.cell_admin_router)
    app.dependency_overrides[require_admin] = verified_admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://cell.example.com"
    ) as client:
        response = await client.post("/v1/admin/cell.settings_set", json={})
    assert response.status_code == expected_status


@pytest.mark.parametrize(
    "extra", ["org", "namespace", "actor", "function", "url", "issued_before", "expires_at", "scim"]
)
def test_global_subject_input_cannot_be_widened_or_restore_access(extra: str) -> None:
    from pydantic import ValidationError

    models = importlib.import_module("server.insights.api.models_cell_admin")
    with pytest.raises(ValidationError):
        models.CellSubjectRevokeInput.model_validate(
            {"context": _context(), "subject": "sa:worker", "reason": "retire", extra: "unexpected"}
        )


@pytest.mark.parametrize("subject", ["", "acme/user:member", "sa:bad\x00key", "s" * 513])
def test_global_subject_input_rejects_ambiguous_or_oversized_subjects(subject: str) -> None:
    from pydantic import ValidationError

    models = importlib.import_module("server.insights.api.models_cell_admin")
    with pytest.raises(ValidationError):
        models.CellSubjectRevokeInput.model_validate({"context": _context(), "subject": subject})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_nested_nonfinite_setting_input_refuses_before_business_coercion(bad: float) -> None:
    from pydantic import ValidationError

    models = importlib.import_module("server.insights.api.models_cell_admin")
    with pytest.raises(ValidationError):
        models.CellSettingsSetInput.model_validate(
            {
                "context": _context(),
                "name": "memory_ask_recency_weight",
                "value": {"nested": [bad]},
                "clear": False,
            }
        )
