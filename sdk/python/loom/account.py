"""The signed-in person's own account, outside the op registry: service
accounts, MCP client setup, the default namespace and the sharing summary —
what the dashboard's Setup and Sharing views do.

sa = await agent.account.create_service_account("ci", ["support"], role="writer")
# sa["token"] is shown once; agent.account.revoke_service_account("ci") ends it
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class AccountClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._agent.transport.request_json("GET", path)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._agent.transport.request_json("POST", path, json=body)

    async def service_accounts(self) -> list[dict[str, Any]]:
        """Your service accounts and MCP client tokens."""
        rows: list[dict[str, Any]] = (await self._get("/v1/me/service_accounts"))["rows"]
        return rows

    async def create_service_account(
        self,
        name: str,
        namespaces: list[str | dict[str, str]],
        *,
        role: str | None = None,
        ttl_days: int | None = 90,
    ) -> dict[str, Any]:
        """A scoped token for a machine, never wider than your own grants.
        `namespaces` in read order; an entry `{"namespace", "role"}` sets its
        own role. The token is in the answer once and never again."""
        body: dict[str, Any] = {"name": name, "namespaces": namespaces, "ttl_days": ttl_days}
        if role is not None:
            body["role"] = role
        return await self._post("/v1/me/service_accounts.create", body)

    async def revoke_service_account(self, name: str) -> dict[str, Any]:
        return await self._post("/v1/me/service_accounts.revoke", {"name": name})

    async def mcp_config(
        self,
        client: str,
        *,
        namespace: str | None = None,
        restricted_namespaces: list[str] | None = None,
        ttl_days: int | None = None,
        persist: bool = False,
    ) -> dict[str, Any]:
        """A fresh token and the config block to paste into an MCP client
        (one of the Setup tab's client keys). The Setup tab saves it to your
        token list (`persist=True`); one not saved lasts at most 30 days and
        cannot be made never to expire (`ttl_days=0`)."""
        body: dict[str, Any] = {"client": client, "persist": persist}
        if namespace is not None:
            body["namespace"] = namespace
        if restricted_namespaces is not None:
            body["restricted_namespaces"] = restricted_namespaces
        if ttl_days is not None:
            body["ttl_days"] = ttl_days
        return await self._post("/v1/me/setup/mcp_config", body)

    async def set_default_namespace(self, namespace: str) -> dict[str, Any]:
        """The namespace MCP sessions use when a call names none."""
        return await self._post("/v1/me/set-default-namespace", {"namespace": namespace})

    async def sharing_summary(self) -> dict[str, Any]:
        """Seven days of who wrote and recalled what, across your namespaces."""
        return await self._get("/v1/me/sharing-summary")
