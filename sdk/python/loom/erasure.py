"""Erasure client. Scoped deletion across all configured backends.

# Erase a single user's data across all namespaces in the org
result = await admin.erasure.request_user_id(
    "alice@example.com", requester="ops@example.com",
)
assert result["status"] == "completed"
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class ErasureClient:
    # Erasure runs inline server-side and can take 30-90s; raise the
    # client timeout above the default so the common path returns the
    # actual outcome instead of a misleading ReadTimeout.
    _ERASURE_TIMEOUT_S = 120.0

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def request(
        self,
        *,
        scope_type: str,
        scope_value: str,
        requester: str,
        urgency: str = "normal",
    ) -> dict[str, Any]:
        """Submit an erasure request. Returns the request payload with steps."""
        payload = {
            "org": self._agent.org,
            "scope_type": scope_type,
            "scope_value": scope_value,
            "requester": requester,
            "urgency": urgency,
        }
        return await self._agent.transport.post(
            "/v1/erasure.request",
            payload,
            timeout_s=self._ERASURE_TIMEOUT_S,
        )

    async def request_user_id(
        self,
        user_id: str,
        *,
        requester: str,
        urgency: str = "normal",
    ) -> dict[str, Any]:
        return await self.request(
            scope_type="user_id",
            scope_value=user_id,
            requester=requester,
            urgency=urgency,
        )

    async def request_namespace(
        self,
        namespace: str,
        *,
        requester: str,
        urgency: str = "normal",
    ) -> dict[str, Any]:
        return await self.request(
            scope_type="namespace",
            scope_value=namespace,
            requester=requester,
            urgency=urgency,
        )

    async def status(self, erasure_id: str) -> dict[str, Any] | None:
        result = await self._agent.transport.post("/v1/erasure.status", {"erasure_id": erasure_id})
        if not result.get("found"):
            return None
        return result

    async def export(
        self,
        *,
        scope_type: str,
        scope_value: str,
        requester: str,
        include_tombstoned: bool = True,
        section_limit: int = 10_000,
    ) -> dict[str, Any]:
        """Export all data Loom holds about a subject (data portability).

        Returns a dict with `counts` and per-section payloads (memory, knowledge,
        agent_events, audit_log). Each section may carry `truncated=True` if it
        exceeded `section_limit`."""
        return await self._agent.transport.post(
            "/v1/erasure.export",
            {
                "org": self._agent.org,
                "scope_type": scope_type,
                "scope_value": scope_value,
                "requester": requester,
                "include_tombstoned": include_tombstoned,
                "section_limit": section_limit,
            },
            timeout_s=self._ERASURE_TIMEOUT_S,
        )

    async def export_user_id(
        self, user_id: str, *, requester: str, include_tombstoned: bool = True
    ) -> dict[str, Any]:
        return await self.export(
            scope_type="user_id",
            scope_value=user_id,
            requester=requester,
            include_tombstoned=include_tombstoned,
        )

    async def export_namespace(self, namespace: str, *, requester: str) -> dict[str, Any]:
        return await self.export(
            scope_type="namespace",
            scope_value=namespace,
            requester=requester,
        )
