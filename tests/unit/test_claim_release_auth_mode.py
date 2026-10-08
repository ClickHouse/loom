"""claim_release gates on the resolved auth mode, like every other admin route.

single-user mode means "no auth at all" even over a prod-derived
LOOM_AUTH_REQUIRED=true; the transport and require_admin already honour that,
so claim_release must too. Hermetic: the ClickHouse client is faked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import server.identity.app.claim_release as cr
from server.settings import settings


class _FakeCH:
    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if "loom.namespaces" in sql:
            return {"org": "acme", "namespace": "vault", "owner_email": "a@acme.test"}
        return {"c": 0}

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        return None


def _release() -> cr.ClaimReleaseOutput:
    no_auth = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    return asyncio.run(
        cr.claim_release(cr.ClaimReleaseInput(org="acme", namespace="vault"), no_auth)
    )


def test_single_user_mode_releases_without_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    monkeypatch.setattr(cr, "get_client", lambda: _FakeCH())

    out = _release()
    assert out.released and out.previous_owner == "a@acme.test"


def test_auth_required_without_bearer_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(cr, "get_client", lambda: _FakeCH())

    with pytest.raises(HTTPException) as exc:
        _release()
    assert exc.value.status_code == 401
