"""An in-process stand-in for the OAuth store (`server.auth.oauth._CHStore`).

Install it with `monkeypatch.setattr(oauth, "_store", MemoryOAuthStore())`;
codes and refresh tokens are kept under their hashes, as the real store does.
"""

from __future__ import annotations

import time
from typing import Any

from server.auth.oauth import _token_id as token_id


class MemoryOAuthStore:
    """The OAuth store in process memory, with `_CHStore`'s async interface."""

    def __init__(self) -> None:
        self.clients: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    async def save_client(self, client_id: str, redirect_uris: list[str], name: str) -> None:
        self.clients[client_id] = {"redirect_uris": list(redirect_uris), "client_name": name}

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self.clients.get(client_id)

    async def save_code(self, code: str, g: dict[str, Any]) -> None:
        self.codes[token_id(code)] = dict(g)

    async def save_refresh(self, token: str, g: dict[str, Any]) -> None:
        self.refresh[token_id(token)] = dict(g)

    async def take_code(self, code: str) -> dict[str, Any] | None:
        g = self.codes.pop(token_id(code), None)
        return None if (g is None or g["exp"] < int(time.time())) else g

    async def take_refresh(self, token: str, client_id: str) -> dict[str, Any] | None:
        g = self.refresh.get(token_id(token))
        if g is None or g["client_id"] != client_id:
            return None
        del self.refresh[token_id(token)]
        return None if g["exp"] < int(time.time()) else g

    async def save_pending(self, state: str, data: dict[str, Any]) -> None:
        self.pending[state] = dict(data)

    async def take_pending(self, state: str) -> dict[str, Any] | None:
        return self.pending.pop(state, None)
