"""Platform-operator-only model setup endpoints.

These routes handle provider secrets, so validation errors deliberately omit
input values and request bodies never enter the operation audit payload.
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError

from server.core.provider_keys import KEY_NAMES, environment_provider_key
from server.insights.api.models_provider_setup import (
    ProviderChangeStatus,
    ProviderResetInput,
    ProviderRetryInput,
    ProviderSaveResult,
    ProviderSetupInput,
    ProviderSetupStatus,
    ProviderValidation,
)
from server.insights.app.admin_routes import refuse_browser_reach, require_admin
from server.insights.app.provider_activation import embedding_change_refusal, save_configuration
from server.insights.app.provider_credentials import open_configuration
from server.insights.app.provider_reset import reset_configuration
from server.insights.app.provider_retry import retry_configuration
from server.insights.app.provider_runtime import environment_embedding, environment_settings
from server.insights.app.provider_validation import validate_providers
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_setup import committed_configuration, pending_configuration
from server.insights.store.provider_configuration import configurations
from server.operations.base import Role


async def require_model_operator(request: Request) -> None:
    auth = await require_admin(request)
    if auth is not None and not auth.has_role(Role.PLATFORM_ADMIN):
        raise HTTPException(403, "Model setup requires a platform administrator")
    refuse_browser_reach(request, "model setup")


provider_router = APIRouter(
    prefix="/v1/admin/models", dependencies=[Depends(require_model_operator)]
)


async def _input[T: BaseModel](request: Request, schema: type[T]) -> T:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(415, "Expected application/json")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            raise HTTPException(413, "Model configuration is too large")
    try:
        return schema.model_validate(json.loads(body))
    except (ValueError, ValidationError):
        raise HTTPException(
            422, "Invalid model configuration; check providers, models and keys"
        ) from None


async def _existing_keys() -> dict[str, str]:
    keys = {name: environment_provider_key(name) for name in KEY_NAMES}
    rows = await configurations()
    active = committed_configuration(rows)
    if active is not None and active["state"] == "active":
        try:
            saved = open_configuration(active["envelope"])
        except ValueError:
            raise HTTPException(
                409, "Saved credentials cannot be read; re-enter provider keys"
            ) from None
        keys.update({provider: value.get_secret_value() for provider, value in saved.keys.items()})
    return keys


def _status_values(saved: ProviderSetupInput | None) -> dict[str, Any]:
    """Describe the committed snapshot, not a replica awaiting its next refresh."""
    environment = environment_settings()
    keys = {name: bool(environment_provider_key(name)) for name in KEY_NAMES}
    if saved:
        keys.update({name: bool(value.get_secret_value()) for name, value in saved.keys.items()})
    llm_provider = saved.llm.provider if saved else environment["llm_provider"]
    embedding_provider = saved.embedding.provider if saved else environment_embedding()[0]
    return {
        "source": "dashboard" if saved else "environment",
        "llm_provider": llm_provider,
        "llm_model": saved.llm.model if saved else environment["llm_model"],
        "embedding_provider": embedding_provider,
        "embedding_model": saved.embedding.model if saved else environment["embedding_model"],
        "embedding_dimensions": saved.embedding.dimensions
        if saved
        else environment["embedding_dim"],
        "embedding_base_url": saved.embedding.base_url
        if saved
        else environment["embedding_base_url"],
        "embedding_send_dimensions": saved.embedding.send_dimensions
        if saved
        else environment["embedding_send_dimensions"],
        "configured_keys": keys,
        "llm_overrides": {str(key): value for key, value in saved.llm.overrides.items()}
        if saved
        else {},
        "verification_models": saved.llm.verification_models if saved else None,
        "requires_setup": (
            llm_provider not in ("openai", "anthropic")
            or embedding_provider not in ("openai", "gemini", "compatible", "local")
            or not keys.get(llm_provider, False)
            or (embedding_provider != "local" and not keys.get(embedding_provider, False))
        ),
    }


@provider_router.post("/validate", response_model=ProviderValidation)
async def validate_model_configuration(request: Request) -> ProviderValidation:
    """Check a model setup against the providers before saving it. An explicit operator
    action: the small model requests it sends may incur provider fees."""
    setup = await _input(request, ProviderSetupInput)
    return await validate_providers(setup, await _existing_keys())


@provider_router.get("", response_model=ProviderSetupStatus)
async def model_configuration_status() -> ProviderSetupStatus:
    """The LLM and embedding settings in force and any change pending, without the saved
    credentials."""
    rows = await configurations()
    active = committed_configuration(rows)
    saved = (
        open_configuration(active["envelope"]) if active and active["state"] == "active" else None
    )
    pending = pending_configuration(rows)
    return ProviderSetupStatus(
        version=str(active["version"]) if active else "0",
        **_status_values(saved),
        pending=ProviderChangeStatus(
            config_id=str(pending["config_id"]),
            state=pending["state"],
            error_code=pending["error_code"],
            progress={
                **{
                    key: value
                    for key, value in json.loads(pending["progress"]).items()
                    if key
                    in {"phase", "table", "table_index", "cursor", "rows_done", "plan_version"}
                    and type(value) in (str, int)
                },
                "table_count": len(REBUILD_TABLES),
            },
        )
        if pending
        else None,
        embedding_change_blocked=await embedding_change_refusal(),
    )


@provider_router.post("", response_model=ProviderSaveResult)
async def save_model_configuration(request: Request) -> ProviderSaveResult:
    """Save the LLM and embedding setup as the next configuration; an embedding change
    queues a rebuild of the vector tables."""
    return await save_configuration(await _input(request, ProviderSetupInput))


@provider_router.post("/reset", response_model=ProviderSaveResult)
async def reset_model_configuration(request: Request) -> ProviderSaveResult:
    """Go back to the model settings the environment gives."""
    return await reset_configuration(await _input(request, ProviderResetInput))


@provider_router.post("/retry", response_model=ProviderSaveResult)
async def retry_model_configuration(request: Request) -> ProviderSaveResult:
    """Run a failed model transition again."""
    return await retry_configuration(await _input(request, ProviderRetryInput))
