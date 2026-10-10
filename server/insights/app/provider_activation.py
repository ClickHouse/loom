"""Persist validated settings; embedding transitions remain staged until rebuilt."""

from datetime import UTC, datetime
from time import time_ns
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException

from server.core.model_work_gate import get_model_work_gate
from server.core.provider_keys import KEY_NAMES, environment_provider_key
from server.embedding.embedder import provider_for
from server.insights.api.models_provider_setup import ProviderSaveResult, ProviderSetupInput
from server.insights.app.provider_credentials import open_configuration, seal_configuration
from server.insights.app.provider_runtime import apply_configuration, environment_embedding
from server.insights.app.provider_validation import validate_providers
from server.insights.domain.provider_rebuild import unpublishable_refusal
from server.insights.domain.provider_setup import committed_configuration, pending_configuration
from server.insights.store.provider_configuration import configurations, write_configuration
from server.insights.store.provider_lock import ConfigurationLockUnavailable, configuration_lock
from server.insights.store.provider_rebuild import (
    rebuild_inventory,
    tables_with_vectors,
    unpublishable_rebuild_tables,
)
from server.settings import settings


async def save_configuration(setup: ProviderSetupInput) -> ProviderSaveResult:
    """Revalidate on save, then compare the revision under a cross-process lock."""
    rows = await configurations()
    active = committed_configuration(rows)
    if setup.expected_version != (str(active["version"]) if active else "0"):
        raise HTTPException(409, "Model settings changed; refresh before saving")
    # Resolve from one persisted revision plus environment fallbacks, never from
    # a replica's possibly stale dashboard key cache.
    existing_keys = {name: environment_provider_key(name) for name in KEY_NAMES}
    if active and active["state"] == "active":
        saved = _open_active(active["envelope"])
        existing_keys.update(
            {str(name): value.get_secret_value() for name, value in saved.keys.items()}
        )
    validation = await validate_providers(setup, existing_keys)
    if not validation.ready:
        raise HTTPException(
            422,
            {
                "code": "MODEL_VALIDATION_FAILED",
                "checks": [c.model_dump() for c in validation.checks],
            },
        )
    try:
        async with configuration_lock():
            return await _save_locked(setup)
    except ConfigurationLockUnavailable:
        raise HTTPException(
            503, "Model settings could not be locked; verify ClickHouse Keeper and retry"
        ) from None


def _open_active(envelope: str) -> ProviderSetupInput:
    try:
        return open_configuration(envelope)
    except ValueError:
        raise HTTPException(
            409, "Saved model settings cannot be decrypted; restore the original signing secret"
        ) from None


async def embedding_change_refusal() -> str:
    """Why this storage refuses an embedding change, or "" when it accepts one."""
    blocked = await unpublishable_rebuild_tables()
    if not blocked or not await tables_with_vectors():
        return ""
    return unpublishable_refusal(blocked)


async def rebuild_required() -> bool:
    """Whether an embedding change must rebuild before it serves. A refused
    transition pauses nothing; with no vectors stored there is nothing to rebuild
    and the change switches directly, each process draining first."""
    try:
        await rebuild_inventory()
    except ValueError:
        raise HTTPException(
            409,
            "Embedding storage differs from the supported schema; "
            "verify migrations and custom vector tables before changing embedding models",
        ) from None
    blocked = await unpublishable_rebuild_tables()
    if not blocked:
        return True
    if await tables_with_vectors():
        raise HTTPException(409, unpublishable_refusal(blocked))
    return False


async def _save_locked(setup: ProviderSetupInput) -> ProviderSaveResult:
    rows = await configurations()
    if pending_configuration(rows) is not None:
        raise HTTPException(409, "Another model configuration is being applied")
    active = committed_configuration(rows)
    version = str(active["version"]) if active else "0"
    if setup.expected_version != version:
        raise HTTPException(409, "Model settings changed; refresh before saving")
    saved_keys: dict[str, str] = {}
    current_embedding = (
        provider_for(settings.embedding_model),
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_base_url.rstrip("/"),
        settings.embedding_send_dimensions,
    )
    local_embedding = current_embedding
    if active and active["state"] == "environment":
        current_embedding = environment_embedding()
    elif active:
        saved = _open_active(active["envelope"])
        saved_keys = {str(name): value.get_secret_value() for name, value in saved.keys.items()}
        # A different replica may have committed settings since this process's
        # last refresh. Compare against the locked persisted revision, not its
        # potentially stale in-process embedding client.
        current_embedding = (
            saved.embedding.provider,
            saved.embedding.model,
            saved.embedding.dimensions,
            saved.embedding.base_url,
            saved.embedding.send_dimensions,
        )
    try:
        envelope = seal_configuration(setup, saved_keys)
    except ValueError:
        raise HTTPException(
            409, "Set a unique LOOM_JWT_SECRET of at least 32 characters before saving keys"
        ) from None
    embedding_changes = (
        setup.embedding.provider,
        setup.embedding.model,
        setup.embedding.dimensions,
        setup.embedding.base_url,
        setup.embedding.send_dimensions,
    ) != current_embedding
    rebuild = embedding_changes and await rebuild_required()
    config_id = uuid4()
    state: Literal["queued", "active"] = "queued" if rebuild else "active"
    await write_configuration(
        {
            "config_id": config_id,
            "parent_id": UUID(str(active["config_id"])) if active else UUID(int=0),
            "state": state,
            "envelope": envelope,
            "progress": "{}",
            "error_code": "",
            "created_at": datetime.now(UTC),
            # Revisions must advance across hosts even with clock skew; readers
            # choose the greatest committed revision, not the newest UUID.
            "version": max(time_ns(), int(version) + 1),
        }
    )
    if (
        not embedding_changes
        and local_embedding == current_embedding
        and not get_model_work_gate().closed
    ):
        # A lagging replica must drain through the runtime reconciler before
        # installing a different vector space, even for a key-only save.
        apply_configuration(open_configuration(envelope))
    return ProviderSaveResult(config_id=str(config_id), state=state)
