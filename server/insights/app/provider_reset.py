"""Versioned return to deployment environment settings, without deleting history."""

import json
from datetime import UTC, datetime
from time import time_ns
from uuid import UUID, uuid4

from fastapi import HTTPException

from server.core.model_work_gate import get_model_work_gate
from server.insights.api.models_provider_setup import ProviderResetInput, ProviderSaveResult
from server.insights.app.provider_activation import _open_active, check_rebuild_inventory
from server.insights.app.provider_runtime import (
    apply_environment_configuration,
    current_embedding,
    environment_embedding,
    environment_settings,
)
from server.insights.domain.provider_setup import committed_configuration, pending_configuration
from server.insights.store.provider_configuration import configurations, write_configuration
from server.insights.store.provider_lock import ConfigurationLockUnavailable, configuration_lock


async def reset_configuration(request: ProviderResetInput) -> ProviderSaveResult:
    """Record reset intent under the same serialization as a dashboard save."""
    try:
        async with configuration_lock():
            return await _reset_locked(request)
    except ConfigurationLockUnavailable:
        raise HTTPException(503, "Model settings could not be locked; retry") from None


async def _reset_locked(request: ProviderResetInput) -> ProviderSaveResult:
    rows = await configurations()
    if pending_configuration(rows) is not None:
        raise HTTPException(409, "Another model configuration is being applied")
    active = committed_configuration(rows)
    version = str(active["version"]) if active else "0"
    if request.expected_version != version:
        raise HTTPException(409, "Model settings changed; refresh before resetting")
    if active is None or active["state"] == "environment":
        return ProviderSaveResult(
            config_id=str(active["config_id"]) if active else "", state="active"
        )
    saved = _open_active(active["envelope"])
    current = (
        saved.embedding.provider,
        saved.embedding.model,
        saved.embedding.dimensions,
        saved.embedding.base_url,
        saved.embedding.send_dimensions,
    )
    rebuild = current != environment_embedding()
    if rebuild:
        await check_rebuild_inventory()
    config_id = uuid4()
    # Only non-secret model settings enter progress. Credentials are always read
    # from each process's environment, never copied into a dashboard override.
    progress = json.dumps(
        {"action": "environment", "environment": json.dumps(environment_settings(), sort_keys=True)}
    )
    await write_configuration(
        {
            "config_id": config_id,
            "parent_id": UUID(str(active["config_id"])),
            "state": "queued" if rebuild else "environment",
            "envelope": "",
            "progress": progress,
            "error_code": "",
            "created_at": datetime.now(UTC),
            "version": max(time_ns(), int(version) + 1),
        }
    )
    if (
        not rebuild
        and current_embedding() == environment_embedding()
        and not get_model_work_gate().closed
    ):
        apply_environment_configuration()
    return ProviderSaveResult(config_id=str(config_id), state="queued" if rebuild else "active")
