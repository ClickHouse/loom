"""An in-process stand-in for the OAuth store (`server.auth.oauth._CHStore`).

Install it with `monkeypatch.setattr(oauth, "store", MemoryOAuthStore())`;
codes, refresh tokens and device codes are kept under their hashes, as the
real store does, and a spent one is kept (marked) so a replay is seen.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI

from server.auth import device, oauth
from server.auth.oauth import CodeReplayed, token_id


def oauth_app() -> FastAPI:
    """Every OAuth route: sign-in, the device grant and MCP connect."""
    app = FastAPI()
    app.include_router(oauth.router)
    app.include_router(device.router)
    app.include_router(oauth.connect_router)
    return app


class MemoryOAuthStore:
    """The OAuth store in process memory, with `_CHStore`'s async interface."""

    def __init__(self) -> None:
        self.clients: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.spent: dict[str, float] = {}  # key -> when it was spent

    async def save_client(self, client_id: str, redirect_uris: list[str], name: str) -> None:
        self.clients[client_id] = {"redirect_uris": list(redirect_uris), "client_name": name}

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self.clients.get(client_id)

    async def save_code(self, code: str, g: dict[str, Any]) -> None:
        self.codes[token_id(code)] = {"family": "", "upstream_refresh": "", **g}
        self.spent.pop(token_id(code), None)

    async def save_refresh(self, token: str, g: dict[str, Any]) -> None:
        self.refresh[token_id(token)] = {"family": "", "upstream_refresh": "", **g}
        self.spent.pop(token_id(token), None)

    def _take(
        self, rows: dict[str, dict[str, Any]], key: str, client_id: str | None = None
    ) -> dict[str, Any] | None:
        g = rows.get(key)
        if g is None or g["exp"] < int(time.time()):
            return None
        if client_id is not None and g["client_id"] != client_id:
            return None
        if key in self.spent:
            raise CodeReplayed({"successor": "", **g})
        self.spent[key] = time.time()
        return dict(g)

    async def take_code(self, code: str, client_id: str) -> dict[str, Any] | None:
        return self._take(self.codes, token_id(code), client_id)

    async def take_refresh(self, token: str, client_id: str) -> dict[str, Any] | None:
        return self._take(self.refresh, token_id(token), client_id)

    async def set_successor(self, token: str, g: dict[str, Any], successor: str) -> None:
        self.refresh[token_id(token)] = {**g, "successor": token_id(successor)}
        self.spent[token_id(token)] = time.time()

    async def retire_unused(self, successor_id: str) -> dict[str, Any] | None:
        g = self.refresh.get(successor_id)
        if g is None or successor_id in self.spent:
            return None
        self.spent[successor_id] = time.time()
        return dict(g)

    async def save_pending(self, state: str, data: dict[str, Any]) -> None:
        self.pending[state] = dict(data)

    async def take_pending(self, state: str) -> dict[str, Any] | None:
        return self.pending.pop(state, None)

    async def save_device(self, d: dict[str, Any]) -> None:
        self.devices[d["id"]] = dict(d)
        self.spent.pop(d["id"], None)  # a new unconsumed version, as the real store writes

    async def device_by_user_code(self, user_code_id: str) -> dict[str, Any] | None:
        for d in self.devices.values():
            if (
                d["user_code"] == user_code_id
                and d["status"] == "pending"
                and d["id"] not in self.spent
                and d["exp"] >= int(time.time())
            ):
                return dict(d)
        return None

    async def take_device(self, device_code: str) -> dict[str, Any] | None:
        d = self.devices.get(token_id(device_code))
        if d is None:
            return None
        if d["exp"] < int(time.time()):
            return dict(d)
        if d["id"] in self.spent:
            raise CodeReplayed(dict(d))
        if d["status"] != "pending":
            self.spent[d["id"]] = time.time()
        return dict(d)
