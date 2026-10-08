"""Explicit retry of the same failed embedding transition and its checkpoint."""

from time import time_ns

from fastapi import HTTPException

from server.insights.api.models_provider_setup import ProviderRetryInput, ProviderSaveResult
from server.insights.domain.provider_setup import pending_configuration
from server.insights.store.provider_configuration import configurations, write_configuration
from server.insights.store.provider_lock import ConfigurationLockUnavailable, configuration_lock


async def retry_configuration(request: ProviderRetryInput) -> ProviderSaveResult:
    try:
        async with configuration_lock():
            pending = pending_configuration(await configurations())
            if pending is None or str(pending["config_id"]) != str(request.config_id):
                raise HTTPException(409, "Model transition changed; refresh before retrying")
            if pending["state"] != "failed":
                raise HTTPException(409, "Only a failed model transition can be retried")
            await write_configuration(
                {
                    **pending,
                    "state": "queued",
                    "error_code": "",
                    "version": max(time_ns(), int(pending["version"]) + 1),
                }
            )
            return ProviderSaveResult(config_id=str(request.config_id), state="queued")
    except ConfigurationLockUnavailable:
        raise HTTPException(503, "Model settings could not be locked; retry") from None
