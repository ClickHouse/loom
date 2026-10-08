"""Audit client. Surfaces tenant-scoped rows from loom.audit_log.

page = await admin.audit.list(event_type="memory.deleted", limit=100)
for row in page["rows"]:
    ...
while page.get("next_cursor"):
    # the cursor carries only the position: pass the filters again
    page = await admin.audit.list(
        event_type="memory.deleted", cursor=page["next_cursor"], limit=100
    )
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class AuditClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def list(
        self,
        *,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "limit": limit,
        }
        if event_type is not None:
            payload["event_type"] = event_type
        if since is not None:
            payload["since"] = since.isoformat()
        if cursor is not None:
            payload["cursor"] = cursor
        return await self._agent.transport.post("/v1/audit.list", payload)
