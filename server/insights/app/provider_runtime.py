"""Load a committed operator configuration before serving or starting workers."""

import asyncio
import json
import logging
from typing import Any

from server.core import deployment_config, embedding_space
from server.core.background import drain_task_sets
from server.core.ch import get_client, set_query_tag
from server.core.model_work_gate import get_model_work_gate
from server.core.provider_keys import install_provider_keys
from server.embedding import embedder
from server.insights.api.models_provider_setup import EmbeddingSetup, ProviderSetupInput
from server.insights.app.provider_credentials import open_configuration
from server.insights.domain.provider_rebuild_manifest import RebuildManifest
from server.insights.domain.provider_serving_policy import serving_vector_settings
from server.insights.domain.provider_setup import (
    committed_configuration,
    committed_descendant,
    llm_setup_settings,
    pending_configuration,
)
from server.insights.store.provider_configuration import configurations
from server.llm import client as llm_client
from server.settings import settings

log = logging.getLogger(__name__)

_applied_version = ""
_environment_settings: dict[str, Any] | None = None
# Why the stored configuration was not applied ('' when it was): /v1/health says so.
_refused = ""
_maintenance_config_id: str | None = None


def environment_settings() -> dict[str, Any]:
    """Capture only model settings before any dashboard configuration is applied.

    Secrets remain in the deployment environment. Returning a copy prevents
    callers from mutating the reset target accidentally.
    """
    global _environment_settings
    if _environment_settings is None:
        fields = set(llm_setup_settings("", "", {}, [])) | {
            "embedding_provider",
            "embedding_model",
            "embedding_dim",
            "embedding_base_url",
            "embedding_send_dimensions",
            "require_real_providers",
            "code_vector_tables",
            "code_search_vector_models",
            "code_search_vector_model_default",
        }
        _environment_settings = settings.model_dump(include=fields)
        _environment_settings["_llm_provider_source"] = settings.llm_provider_source()
    return dict(_environment_settings)


def _environment_embedding() -> dict[str, Any] | None:
    """The env's embedding settings once captured: a dashboard configuration may
    have replaced the live ones since."""
    if _environment_settings is None:
        return None
    return {name: _environment_settings[name] for name in embedding_space.FIELDS}


deployment_config.set_environment_source(_environment_embedding)


def apply_environment_configuration() -> None:
    """Clear dashboard overrides; embedding transitions must already be rebuilt."""
    global _refused
    _refused = ""  # the environment's settings passed the gateway check at load
    for name, value in environment_settings().items():
        setattr(settings, name, value)
    install_provider_keys({})
    llm_client.reset()
    embedder.reset()


def environment_embedding() -> embedding_space.Identity:
    return embedding_space.identity(environment_settings())


def current_embedding() -> embedding_space.Identity:
    return embedding_space.identity(settings.model_dump())


def gateway_refusal(embedding: EmbeddingSetup) -> str:
    """Why a configuration with this embedding would send model calls off the
    gateway, as the settings check it at load; "" when it would not."""
    return settings.gateway_refusal(
        {
            "embedding_provider": embedding.provider,
            "embedding_model": embedding.model,
            "embedding_base_url": embedding.base_url,
        }
    )


def refused_configuration() -> str:
    """Why the stored configuration is not applied; "" when it is."""
    return _refused


def apply_configuration(setup: ProviderSetupInput) -> None:
    """Apply a validated snapshot with no await points or environment mutation.

    Call at startup, or only after admission and workers have drained for a model
    transition. Embedding changes must not be applied to a live old-model index.
    Its callers have run the gateway check (`_refuse`, `_save_locked`).
    """
    environment_settings()
    values = llm_setup_settings(
        setup.llm.provider,
        setup.llm.model,
        {str(name): model for name, model in setup.llm.overrides.items()},
        setup.llm.verification_models,
    )
    for name, value in values.items():
        setattr(settings, name, value)
    settings.embedding_provider = setup.embedding.provider
    settings.embedding_model = setup.embedding.model
    settings.embedding_dim = setup.embedding.dimensions
    settings.embedding_base_url = setup.embedding.base_url
    settings.embedding_send_dimensions = setup.embedding.send_dimensions
    settings.require_real_providers = True
    settings._llm_provider_source = "dashboard"
    install_provider_keys(
        {provider: secret.get_secret_value() for provider, secret in setup.keys.items()}
    )
    llm_client.reset()
    embedder.reset()


def _active_setup(active: dict[str, Any]) -> ProviderSetupInput | None:
    if active["state"] == "environment":
        expected = json.loads(active.get("progress", "{}")).get("environment")
        if expected and embedding_space.identity(json.loads(expected)) != environment_embedding():
            raise RuntimeError(
                "Environment embedding settings differ from the committed reset; "
                "restore the prior environment or rebuild before switching embedding spaces."
            )
        return None
    try:
        return open_configuration(active["envelope"])
    except ValueError:
        raise RuntimeError(
            "Saved model configuration cannot be decrypted; restore the signing secret "
            "used to save it before starting Loom."
        ) from None


def _hold_admission(config_id: str, refusal: str = "") -> None:
    """Close admission for `config_id`; a transition already holding it (a
    refused configuration's hold, an earlier change) hands it over. `refusal`
    is set for a refused configuration's hold and empty for a transition's."""
    global _maintenance_config_id
    gate = get_model_work_gate()
    if _maintenance_config_id is not None and _maintenance_config_id != config_id:
        gate.transfer(_maintenance_config_id, config_id, refusal=refusal)
    else:
        gate.close(config_id, refusal=refusal)
    _maintenance_config_id = config_id


async def _refresh_calibration() -> None:
    """Never reuse the previous embedding space's similarity thresholds."""
    from server.core.vectors import code_tables, codes_state
    from server.memory.app import embedder_calibration

    code_tables.reset()
    codes_state.reset()
    # Read again even when a reading landed meanwhile: a background probe that
    # started before the rebuild may have published the old widths.
    await codes_state.ensure_probed(get_client(), reread=True)

    embedder_calibration.set_calibration(None)
    embedder_calibration.clear_ns_overrides()
    if settings.adaptive_thresholds:
        # The new embedder's shared row when one exists, else a probe.
        await embedder_calibration.adopt_or_probe()
        embedder_calibration.start_refresh_loop()


def _serving_settings(
    rows: list[dict[str, Any]], active: dict[str, Any], target: tuple[str, str, int, str, bool]
) -> dict[str, str]:
    """Key rotations inherit the nearest completed embedding recipe in their chain."""
    by_id = {str(row["config_id"]): row for row in rows}
    seen = set()
    row: dict[str, Any] | None = active
    while row is not None:
        identity = str(row["config_id"])
        if identity in seen:
            raise ValueError("Cyclic provider serving configuration")
        seen.add(identity)
        progress = json.loads(row.get("progress", "{}"))
        if "manifest" in progress:
            manifest = RebuildManifest.from_dict(progress["manifest"])
            manifest.require_prepared()
            if (
                str(manifest.job_id) != identity
                or row["state"] not in ("active", "environment")
                or progress.get("phase") != "activate"
                or manifest.embedding_fingerprint != embedding_space.fingerprint(*target)
            ):
                raise ValueError("Serving recipe differs from committed embedding space")
            return serving_vector_settings(
                target[1],
                target[2],
                manifest.prefix_dimensions,
                prior_default=settings.code_search_vector_model_default,
            )
        row = by_id.get(str(row.get("parent_id", "")))
    return {}


async def load_active_configuration() -> None:
    """Keep unfinished rebuilds closed across refreshes and process restarts.

    This local reconciliation is not a distributed drain acknowledgement. The
    coordinator must separately await deferred writes and all other replicas.
    """
    environment_settings()
    rows = await configurations()
    pending = pending_configuration(rows)
    if pending is not None:
        _hold_admission(str(pending["config_id"]))
    active = committed_configuration(rows)
    global _applied_version, _maintenance_config_id
    if active is None or str(active["version"]) == _applied_version:
        return
    setup = _active_setup(active)
    target = (
        environment_embedding()
        if setup is None
        else (
            setup.embedding.provider,
            setup.embedding.model,
            setup.embedding.dimensions,
            setup.embedding.base_url,
            setup.embedding.send_dimensions,
        )
    )
    changed = target != current_embedding()
    if setup is not None and _refuse(setup, pending is None, str(active["config_id"])):
        return  # held or kept, retried at the next refresh
    if changed and pending is None and _maintenance_config_id is None:
        _hold_admission(str(active["config_id"]))
    serving = _serving_settings(rows, active, target)
    gate = get_model_work_gate()
    if _maintenance_config_id is not None:
        if gate.active or not await drain_task_sets(timeout_s=0):
            return  # Existing work finishes with its old clients; retry next refresh.
        if pending is None and not committed_descendant(
            rows, str(active["config_id"]), _maintenance_config_id
        ):
            raise RuntimeError("Model transition has no matching committed configuration")
    if setup is None:
        apply_environment_configuration()
    else:
        apply_configuration(setup)
    for name, value in serving.items():
        setattr(settings, name, value)
    if pending is None and _maintenance_config_id is not None:
        from server.embedding._cache import clear_for_model_transition

        clear_for_model_transition()
        # Retry calibration even if a previous attempt already installed the
        # target clients before failing. Admission remains closed meanwhile.
        await _refresh_calibration()
        gate.resume(_maintenance_config_id)
        _maintenance_config_id = None
    _applied_version = str(active["version"])


def _refuse(setup: ProviderSetupInput, hold: bool, config_id: str) -> bool:
    """Whether the gateway check refuses the committed `setup`, as it refuses
    it at load. Then nothing of it applies, its serving vector settings
    included. The check reads only the embedding, so a refused one is never
    the one in force: the index may be built for that other space, and
    unless a queued row holds requests already (`hold` false), model work
    stays held until a configuration the check accepts replaces it."""
    global _refused
    refusal = gateway_refusal(setup.embedding)
    if refusal and refusal != _refused:
        log.error("saved model configuration not applied: %s", refusal)
    _refused = refusal
    if refusal and hold:
        # An ordinary transition hold (taken over from any change still holding
        # it): what replaces it takes it over, and its commit resumes it. Until
        # then requests are refused, not asked to retry.
        _hold_admission(config_id, refusal)
    return bool(refusal)


async def refresh_loop() -> None:
    """Every web/worker process sees committed key and model rotations."""
    set_query_tag("boot.provider_configuration_refresh")
    while True:
        await asyncio.sleep(3)
        try:
            await load_active_configuration()
            from server.insights.app.provider_coordinator import coordinate_once

            await coordinate_once()
        except Exception:  # noqa: BLE001 — retain last good settings on transient DB/decryption failures
            log.warning(
                "Model configuration refresh failed; retaining last successfully loaded settings"
            )
