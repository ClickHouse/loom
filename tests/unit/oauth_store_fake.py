"""The OAuth store with Keeper and ClickHouse held in process memory.

Install it with `monkeypatch.setattr(oauth, "store", MemoryOAuthStore())`. It
is the real store (`server.auth.oauth_store.ClickHouseStore`) with only its
storage swapped: Keeper is a dict with the same compare-and-set, and the few
ClickHouse rows (clients, refresh grants, owed revocations) are dicts.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI

from server.auth import claims, device, oauth, password_setup
from server.auth.oauth_store import ClickHouseStore, Grant


def oauth_app() -> FastAPI:
    """Every OAuth route: sign-in, the device grant and MCP connect."""
    app = FastAPI()
    app.add_exception_handler(oauth.OAuthError, oauth.oauth_error_response)
    app.add_exception_handler(claims.Busy, oauth._busy_response)
    app.include_router(oauth.router)
    app.include_router(device.router)
    app.include_router(password_setup.router)
    app.include_router(oauth.connect_router)
    return app


class MemoryOAuthStore(ClickHouseStore):
    def __init__(self) -> None:
        self.keeper: dict[str, str] = {}
        self.clients: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, Grant] = {}  # by token id
        self.owed: set[str] = set()

    async def _get(self, key: str) -> str | None:
        return self.keeper.get(key)

    async def _swap(self, key: str, old: str | None, new: str, expires_at: int) -> str | None:
        if self.keeper.get(key) == old:
            self.keeper[key] = new
        return self.keeper.get(key)

    async def save_client(self, client_id: str, redirect_uris: list[str], name: str) -> None:
        self.clients[client_id] = {"redirect_uris": list(redirect_uris), "client_name": name}

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self.clients.get(client_id)

    async def _save_refresh_grant(self, rid: str, g: Grant) -> None:
        self.refresh[rid] = {k: v for k, v in g.items() if k != "held"}  # type: ignore[assignment]

    async def _refresh_grant(self, rid: str) -> Grant | None:
        g = self.refresh.get(rid)
        return dict(g) if g else None  # type: ignore[return-value]

    async def owe_revocation(self, family: str, reason: str) -> None:
        self.owed.add(family)

    async def session_ended(self, family: str) -> bool:
        from server.auth import revocation

        return revocation.is_revoked(family) or family in self.owed

    def records(self, kind: str) -> dict[str, dict[str, Any]]:
        """The `kind` records in Keeper ("code", "device"…), by id."""
        prefix = f"{kind}:"
        return {
            k.removeprefix(prefix): json.loads(v)
            for k, v in self.keeper.items()
            if k.startswith(prefix)
        }

    def change(self, kind: str, **fields: Any) -> None:
        """Edit every `kind` record in Keeper in place (an expiry passing…)."""
        for key, value in self.keeper.items():
            if key.startswith(f"{kind}:"):
                self.keeper[key] = json.dumps(json.loads(value) | fields)
