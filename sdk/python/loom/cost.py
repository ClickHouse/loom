"""Cost client. Surfaces per-tenant op-level rollups for ops dashboards.

summary = await agent.cost.summary(
    from_date=date(2026, 5, 1),
    to_date=date(2026, 5, 13),
    group_by=["operation"],
)
# summary["rows"] → [{operation, ops, errors, tokens, cost_usd, latency_p99_ms, ...}]
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class CostClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def summary(
        self,
        *,
        from_date: date,
        to_date: date,
        group_by: list[str] | None = None,
        namespace: str | None | object = ...,
        agent: str | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Default: per-namespace summary scoped to the calling Agent's
        namespace. Pass `namespace=None` for an org-wide rollup."""
        ns: str | None
        if namespace is ...:
            ns = self._agent.namespace  # default: caller's own ns
        else:
            ns = namespace  # type: ignore[assignment]
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "group_by": group_by if group_by is not None else ["operation"],
        }
        if ns is not None:
            payload["namespace"] = ns
        if agent is not None:
            payload["agent"] = agent
        if operation is not None:
            payload["operation"] = operation
        return await self._agent.transport.post("/v1/cost.summary", payload)
