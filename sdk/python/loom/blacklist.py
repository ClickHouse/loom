"""Blacklist client — patterns that should never become memory.

The fifth belief-revision operation: "Refuse." A pattern committed
through this surface gets enforced at every `memory.set` /
`memory.update_by_id` call — matching writes get rejected with
422 BLACKLISTED. `memory.search` also filters matching rows from
results as defense-in-depth.

Scope levels:
  - "namespace": applies only when (org, namespace) match
  - "org":       applies across every namespace under org
  - "global":    applies everywhere (admin-only typically)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from loom.agent import Agent

PatternType = Literal["substring", "regex"]
Scope = Literal["namespace", "org", "global"]


class BlacklistClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def add(
        self,
        pattern: str,
        *,
        pattern_type: PatternType = "substring",
        scope: Scope = "namespace",
        reason: str = "",
        created_by: str = "",
        sweep: bool = False,
    ) -> dict[str, Any]:
        """Add a blacklist pattern. WRITER role required.

        `sweep=True` also tombstones the live rows the pattern already
        matches, up to 5,000 per call; `swept_truncated` in the response
        says to call `agent.call("blacklist.sweep", {"pattern_id": ...})`
        for the rest."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "pattern": pattern,
            "pattern_type": pattern_type,
            "scope": scope,
            "reason": reason,
            "created_by": created_by,
        }
        if sweep:
            payload["sweep"] = True
        return await self._agent.transport.post("/v1/blacklist.add", payload)

    async def list(
        self,
        *,
        scope: Scope | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List patterns applicable to (org, namespace). When `scope`
        is None, returns the EFFECTIVE rule set — namespace + org +
        global patterns that would actually fire for this caller."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "limit": limit,
        }
        if scope is not None:
            payload["scope"] = scope
        r = await self._agent.transport.post("/v1/blacklist.list", payload)
        return list(r.get("rows", []))

    async def remove(
        self,
        pattern_id: str,
        *,
        removed_by: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Soft-delete a pattern. ADMIN. Cache invalidates immediately."""
        return await self._agent.transport.post(
            "/v1/blacklist.remove",
            {
                "org": self._agent.org,
                "pattern_id": pattern_id,
                "removed_by": removed_by,
                "reason": reason,
            },
        )
