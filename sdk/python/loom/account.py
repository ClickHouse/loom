"""The signed-in person's own account, outside the op registry: their tokens,
the default namespace and the sharing summary — what the dashboard's Settings
and Sharing views do.

t = await agent.account.create_token()  # reads everything you belong to
ci = await agent.account.create_token(["support", "docs"], name="ci", role="viewer")
# t["token"] is shown once, with t["clients"] (each MCP client's setup);
# agent.account.revoke_token("ci") ends it
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from loom.agent import Agent


class AccountClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._agent.transport.request_json("GET", path)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._agent.transport.request_json("POST", path, json=body)

    async def tokens(self) -> list[dict[str, Any]]:
        """The tokens you made, and other people's that reach a namespace you
        administer, newest first; each row's `yours` marks your own."""
        rows: list[dict[str, Any]] = (await self._get("/v1/me/service_accounts"))["rows"]
        return rows

    async def create_token(
        self,
        namespaces: Literal["*"] | list[str | dict[str, str]] = "*",
        *,
        name: str | None = None,
        role: str | None = None,
        ttl_days: int | None = 90,
    ) -> dict[str, Any]:
        """A token for your tools, agents, scripts or CI, never wider than your
        own grants. `namespaces`: "*" reads every namespace you belong to, now
        and later, at your role in each (name one before "*", as in
        `["notes", "*"]`, to save there when it may, and restricted ones beside
        it to read them too). A list without "*" reads exactly those, in read
        order; an entry `{"namespace", "role"}` sets its own role and `role` the
        role of bare entries (a 422 with "*"). `name`: None names it
        `token-<day>-<hex>`. `ttl_days`: None for 90 days, 0 for never. The token
        is in the answer once and never again, with `saves_to` (where calls that
        name no namespace save) and `clients` (each MCP client's setup)."""
        body: dict[str, Any] = {"namespaces": namespaces, "ttl_days": ttl_days}
        if name is not None:
            body["name"] = name
        if role is not None:
            body["role"] = role
        return await self._post("/v1/me/service_accounts.create", body)

    async def revoke_token(self, name: str) -> dict[str, Any]:
        """End the token `name` now, and the grants it was given."""
        return await self._post("/v1/me/service_accounts.revoke", {"name": name})

    async def set_default_namespace(self, namespace: str) -> dict[str, Any]:
        """The namespace MCP sessions use when a call names none."""
        return await self._post("/v1/me/set-default-namespace", {"namespace": namespace})

    async def sharing_summary(self) -> dict[str, Any]:
        """Seven days of who wrote and recalled what, across your namespaces."""
        return await self._get("/v1/me/sharing-summary")
