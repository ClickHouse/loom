"""The /me Setup tab mints an org-wide (`*`) token with the caller's flat role. A scoped
token (an `ns_roles` map, no flat role) is refused: minting from it would turn a
per-namespace viewer into an org-wide writer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth.jwt import AuthInfo
from server.identity.app import me_setup as me
from server.operations.base import Role


def _request() -> Any:
    return SimpleNamespace(headers={"Authorization": "Bearer t"})


def test_a_scoped_token_cannot_mint_a_setup_token(monkeypatch) -> None:
    scoped = AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("a",),
        roles=(),
        is_service_account=True,
        ns_roles=(("a", "viewer"),),
    )
    monkeypatch.setattr("server.auth.bearer.verify_bearer", lambda token: scoped)
    with pytest.raises(HTTPException) as e:
        me._setup_identity(_request(), {})
    assert e.value.status_code == 403


def test_a_flat_token_keeps_its_own_roles(monkeypatch) -> None:
    flat = AuthInfo(
        subject="alice",
        org="o",
        namespaces=("home",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    monkeypatch.setattr("server.auth.bearer.verify_bearer", lambda token: flat)
    assert me._setup_identity(_request(), {}) == ("alice", "o", "home", ["viewer"])
