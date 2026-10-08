"""Per-namespace RBAC grants client.

A JWT's role applies uniformly to every namespace it covers. Grants
elevate one subject on one specific (org, namespace) without re-minting
the token — useful for the "invite Alice as Maintainer on this datapack"
workflow.

Roles map: viewer = read-only, writer = read+write, admin = read+write+promote.

Grants are additive — they can only elevate, never restrict. A JWT
already carrying ADMIN keeps ADMIN regardless of grants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from loom.agent import Agent

GrantRole = Literal["viewer", "writer", "admin"]


class GrantsClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def create(
        self,
        subject: str,
        role: GrantRole,
        *,
        granted_by: str = "admin",
        replace: bool = False,
    ) -> dict[str, Any]:
        """Grant `subject` `role` on this client's bound namespace.

        Re-creating at the same role or a higher one is fine. A role BELOW the
        subject's current active grant is refused with 409
        ``GRANT_WOULD_DOWNGRADE`` unless ``replace=True`` — the table replaces
        on (org, namespace, subject), so the default protects an admin from
        being lowered by an onboarding script that meant to add a viewer.
        `granted_by` is honoured only with auth off; under auth the server
        stamps the caller.
        """
        return await self._agent.transport.post(
            "/v1/grants.create",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "subject": subject,
                "role": role,
                "granted_by": granted_by,
                "replace": replace,
            },
        )

    async def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Live grants on this namespace (revoked ones filtered out)."""
        result = await self._agent.transport.post(
            "/v1/grants.list",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "limit": limit,
            },
        )
        rows: list[dict[str, Any]] = result.get("rows", [])
        return rows

    async def revoke(self, subject: str) -> dict[str, Any]:
        """Revoke `subject`'s grant on this namespace. Idempotent: returns
        `revoked=False` if there was nothing to revoke."""
        return await self._agent.transport.post(
            "/v1/grants.revoke",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "subject": subject,
            },
        )
