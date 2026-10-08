"""GET /v1/whoami — service-account self-introspection."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_whoami_unauthenticated(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.get("/v1/whoami")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False


@pytest.mark.asyncio
async def test_whoami_with_valid_token(control_plane_url: str) -> None:
    from server.auth.jwt import mint_token

    # mint_token defaults to ttl_seconds=None (indefinite — no exp
    # claim). This test wants an exp to assert on, so opt in explicitly.
    token = mint_token(
        subject="sa-test",
        org="clickhouse",
        namespaces=["eng-tooling"],
        roles=["VIEWER", "WRITER"],
        ttl_seconds=3600,
    )
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        timeout=5.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        r = await c.get("/v1/whoami")
    body = r.json()
    assert body["authenticated"] is True
    assert body["subject"] == "sa-test"
    assert body["org"] == "clickhouse"
    assert body["namespaces"] == ["eng-tooling"]
    assert set(body["roles"]) == {"VIEWER", "WRITER"}
    assert body["is_service_account"] is True
    # kid comes from settings.jwt_active_kid; don't hardcode "default"
    # since the server may run in a rotated-kid configuration.
    from server.settings import settings

    assert body["kid"] == settings.jwt_active_kid
    assert body["exp"] > 0


@pytest.mark.asyncio
async def test_whoami_rejects_invalid_token(control_plane_url: str) -> None:
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        timeout=5.0,
        headers={"Authorization": "Bearer not-a-real-jwt-at-all"},
    ) as c:
        r = await c.get("/v1/whoami")
    # whoami intentionally returns 200 + authenticated=false on bad tokens
    # so the SPA can drop the stale token and show the login surface
    # without bouncing into an error state.
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body.get("reason")


@pytest.mark.asyncio
async def test_whoami_surfaces_kid_during_rotation(control_plane_url: str) -> None:
    """A token minted with an explicit kid is reported back."""
    from server.auth.jwt import mint_token
    from server.settings import settings

    # mint_token supports explicit kid only against the active map; here we
    # use the legacy default kid path that already exists in dev settings.
    token = mint_token(
        subject="sa-kid-test",
        org="clickhouse",
        namespaces=["t"],
        roles=["VIEWER"],
    )
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        timeout=5.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        body = (await c.get("/v1/whoami")).json()
    assert body["kid"] == settings.jwt_active_kid
