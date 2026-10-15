"""The deployment itself, outside the op registry: health, background
workers, the model and provider settings, and releasing a self-signup claim
— the dashboard's Admin views. Most need a platform or org admin token.
(The admin *operations*, like `admin.users`, are on `agent.client.admin`.)

status = await agent.deployment.workers_status()
models = await agent.deployment.models()  # then validate_models / save_models
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class DeploymentClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._agent.transport.request_json(method, path, json=body)

    async def health(self) -> dict[str, Any]:
        """What /v1/health reports: the store, the workers, problems and their fixes."""
        return await self._call("GET", "/v1/health")

    async def workers_status(self) -> dict[str, Any]:
        """Each background worker: enabled, interval, last run, why it is off."""
        return await self._call("GET", "/v1/admin/workers.status")

    async def models(self) -> dict[str, Any]:
        """The LLM and embedding settings in force, and any change pending."""
        return await self._call("GET", "/v1/admin/models")

    async def validate_models(self, setup: dict[str, Any]) -> dict[str, Any]:
        """Check a model setup (the `models()` shape: llm, embedding, keys,
        expected_version) against the providers before saving it."""
        return await self._call("POST", "/v1/admin/models/validate", setup)

    async def save_models(self, setup: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/v1/admin/models", setup)

    async def reset_models(self, expected_version: str) -> dict[str, Any]:
        """Back to the settings the environment gives."""
        return await self._call(
            "POST", "/v1/admin/models/reset", {"expected_version": expected_version}
        )

    async def retry_models(self, config_id: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/admin/models/retry", {"config_id": config_id})

    async def release_claim(self, org: str, namespace: str, reason: str) -> dict[str, Any]:
        """Release a self-signup claim: revoke its grants, clear the owner, deactivate it."""
        return await self._call(
            "POST",
            "/v1/admin/claim_release",
            {"org": org, "namespace": namespace, "reason": reason},
        )
