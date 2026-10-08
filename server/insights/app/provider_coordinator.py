"""Run queued embedding transitions outside the model-work admission gate."""

import json
from time import time_ns
from typing import Any
from uuid import UUID

from server.core.secrets_at_rest import seal, unseal
from server.insights.app.provider_barrier import acknowledge_drain, distributed_drain
from server.insights.app.provider_credentials import open_configuration
from server.insights.app.provider_rebuild_backend import ClickHouseRebuildBackend
from server.insights.app.provider_rebuild_runner import run_rebuild_step
from server.insights.app.provider_rebuild_target import rebuild_target
from server.insights.app.provider_runtime import environment_embedding
from server.insights.domain.provider_serving_policy import embedding_space_fingerprint
from server.insights.domain.provider_setup import pending_configuration
from server.insights.domain.provider_views import RebuildView
from server.insights.store.provider_configuration import configurations, write_configuration
from server.insights.store.provider_lock import ConfigurationLockUnavailable, configuration_lock
from server.insights.store.provider_views import capture_rebuild_views
from server.settings import settings


def _target(row: dict[str, Any]) -> tuple[Any, tuple[str, str, int, str, bool]]:
    progress = json.loads(row["progress"])
    if progress.get("action") == "environment":
        environment = json.loads(progress["environment"])
        identity = (
            str(environment["embedding_provider"]),
            str(environment["embedding_model"]),
            int(environment["embedding_dim"]),
            str(environment["embedding_base_url"]).rstrip("/"),
            bool(environment["embedding_send_dimensions"]),
        )
        actual = environment_embedding()
        if not identity[0]:
            identity = (actual[0], *identity[1:])
        if identity != actual:
            raise ValueError("Environment reset target changed during rebuild")
        return None, identity
    setup = open_configuration(row["envelope"])
    embedding = setup.embedding
    return setup, (
        embedding.provider,
        embedding.model,
        embedding.dimensions,
        embedding.base_url,
        embedding.send_dimensions,
    )


async def _freeze_recipe(
    row: dict[str, Any], identity: tuple[str, str, int, str, bool]
) -> dict[str, Any]:
    progress = json.loads(row["progress"])
    fingerprint = embedding_space_fingerprint(*identity)
    if "recipe" in progress:
        if progress["recipe"]["fingerprint"] != fingerprint:
            raise ValueError("Frozen rebuild target differs from configuration")
        return row
    mapping = json.loads(settings.code_vector_tables or "{}")
    widths = sorted(
        {
            int(value["dim"])
            for label, value in mapping.items()
            if label.startswith("prefix:")
            and value["table"] == "loom.code_chunks_prefix"
            and 1 <= int(value["dim"]) <= identity[2]
        }
    )
    views = await capture_rebuild_views()
    # Operator-authored DDL can itself contain secrets. Encrypt the whole view
    # recipe, not just provider keys; it never enters the public progress shape.
    progress["recipe"] = {
        "fingerprint": fingerprint,
        "prefix_dimensions": widths,
        "views": seal("provider-rebuild-views", json.dumps([view.to_dict() for view in views])),
    }
    updated = {
        **row,
        "progress": json.dumps(progress, sort_keys=True),
        "version": max(time_ns(), int(row["version"]) + 1),
    }
    await write_configuration(updated)
    return updated


async def coordinate_once() -> None:
    """One restart-safe step. Never steal from a merely unreachable process."""
    candidate = pending_configuration(await configurations())
    if candidate is None:
        return
    transition = UUID(str(candidate["config_id"]))
    if not await acknowledge_drain(transition) or candidate["state"] == "failed":
        return
    # A read-replica acknowledges the drain like every participant and leaves
    # the rebuild to the main Loom, whose workers do the work.
    if settings.read_replica:
        return
    try:
        async with configuration_lock():
            current = pending_configuration(await configurations())
            if current is None or UUID(str(current["config_id"])) != transition:
                return
            if current["state"] == "failed" or not await distributed_drain(transition):
                return
            try:
                setup, identity = _target(current)
                target = rebuild_target(identity, setup)
            except (ValueError, RuntimeError):
                # Configuration/key errors happen before any staging effects.
                # Preserve the last cursor, exposing no secret-bearing error.
                await write_configuration(
                    {
                        **current,
                        "state": "failed",
                        "error_code": "rebuild_target_failed",
                        "version": max(time_ns(), int(current["version"]) + 1),
                    }
                )
                return
            try:
                current = await _freeze_recipe(current, identity)
                recipe = json.loads(current["progress"])["recipe"]
                plain_views = unseal("provider-rebuild-views", recipe["views"])
                if not plain_views:
                    raise ValueError("Rebuild view recipe cannot be decrypted")
                views = tuple(RebuildView.from_dict(value) for value in json.loads(plain_views))
            except (ValueError, KeyError, TypeError):
                # Unsupported DDL or a corrupt recipe needs an explicit retry,
                # not an indefinitely queued job with no actionable UI state.
                await write_configuration(
                    {
                        **current,
                        "state": "failed",
                        "error_code": "rebuild_recipe_failed",
                        "version": max(time_ns(), int(current["version"]) + 1),
                    }
                )
                return
            backend = ClickHouseRebuildBackend(
                transition,
                recipe["fingerprint"],
                target,
                prefix_dimensions=tuple(recipe["prefix_dimensions"]),
                barrier=distributed_drain,
                views=views,
            )
            await run_rebuild_step(current, backend)
    except ConfigurationLockUnavailable:
        return
