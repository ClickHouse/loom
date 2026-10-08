"""JWT auth: token roundtrip + auth-required mode.

Two test layers:
1. Unit-shaped: mint_token → verify_token roundtrip, no HTTP.
2. ASGI in-process: spin up the FastAPI app with auth_required=True and
   confirm 401/403 behavior. Uses httpx ASGITransport so we don't need
   a separate server process.
"""

from __future__ import annotations

import pytest

# ============================================================
# Unit-shaped tests on the JWT mint/verify path
# ============================================================


def test_mint_then_verify_roundtrip() -> None:
    from server.auth.jwt import mint_token, verify_token

    token = mint_token(
        subject="detector",
        org="clickhouse",
        namespaces=["eng-tooling"],
        roles=["WRITER"],
    )
    info = verify_token(token)
    assert info.subject == "detector"
    assert info.org == "clickhouse"
    assert info.namespaces == ("eng-tooling",)
    assert info.is_service_account is True
    role_names = [r.name for r in info.roles]
    assert "WRITER" in role_names


def test_verify_rejects_expired_token() -> None:
    from server.auth.jwt import TokenError, mint_token, verify_token
    from server.settings import settings

    # Past the configured clock-skew leeway. A token only seconds past
    # exp is intentionally accepted (jwt_leeway_seconds, default 30).
    expired = mint_token(
        subject="x",
        org="o",
        namespaces=["n"],
        roles=["VIEWER"],
        ttl_seconds=-(settings.jwt_leeway_seconds + 60),
    )
    with pytest.raises(TokenError):
        verify_token(expired)


def test_verify_rejects_wrong_signature() -> None:
    from server.auth.jwt import TokenError, mint_token, verify_token

    token = mint_token(
        subject="x",
        org="o",
        namespaces=["n"],
        roles=["VIEWER"],
        secret="other-secret",
    )
    with pytest.raises(TokenError):
        verify_token(token)  # uses settings.jwt_secret, mismatched


def test_auth_info_role_check() -> None:
    from server.auth.authz import Role
    from server.auth.jwt import AuthInfo

    auth = AuthInfo(
        subject="x",
        org="o",
        namespaces=("n",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )
    assert auth.has_role(Role.VIEWER) is True  # writer >= viewer
    assert auth.has_role(Role.WRITER) is True
    assert auth.has_role(Role.ADMIN) is False
    assert auth.can_access("o", "n") is True
    assert auth.can_access("o", "other") is False
    assert auth.can_access("other-org", "n") is False


# ============================================================
# ASGI in-process tests with auth_required=True
# ============================================================


@pytest.mark.asyncio
async def test_auth_required_rejects_missing_token(monkeypatch) -> None:
    """With auth_required=True, missing Authorization → 401."""
    # LOOM_IDENTITY_PROVIDER=single-user in .env short-circuits
    # auth_actually_required() to False regardless of auth_required.
    # Patch the live singleton directly rather than reloading
    # server.settings: reload() rebinds the module-level Settings
    # instance, but every server module that did
    # `from server.settings import settings` at import time keeps the
    # OLD reference. The test then runs against a new singleton the
    # production code can't see, AND leaks the new singleton's stale
    # state to every subsequent test (notably tests/unit that depend
    # on settings.llm_provider, which then reads "openai" stranded
    # from this monkeypatch's env). monkeypatch.setattr mutates the
    # live object in place; pytest auto-restores at finalizer time.
    import server.main
    from server.settings import settings

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")

    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.main.app),
        base_url="http://test",
    ) as client:
        # NB: lifespan isn't invoked by ASGITransport by default. Health is
        # fine since it doesn't touch CH; we just want to assert 401 routing.
        resp = await client.post(
            "/v1/memory.get",
            json={"org": "x", "namespace": "y", "agent": "z", "memory_key": "k"},
        )
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"]["code"] == "AUTH_INVALID"


@pytest.mark.asyncio
async def test_auth_required_rejects_bad_token(monkeypatch) -> None:
    """Auth header with garbage → 401."""
    import server.main
    from server.settings import settings

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")

    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.main.app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/v1/memory.get",
            json={"org": "x", "namespace": "y", "agent": "z", "memory_key": "k"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_required_rejects_wrong_namespace(monkeypatch) -> None:
    """Token grants access to ns1 but caller asks about ns2 → 403."""
    import server.main
    from server.settings import settings

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")

    from server.auth.jwt import mint_token

    token = mint_token(
        subject="agent-x",
        org="clickhouse",
        namespaces=["ns1"],
        roles=["VIEWER"],
    )

    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.main.app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/v1/memory.get",
            json={
                "org": "clickhouse",
                "namespace": "ns2",  # not granted
                "agent": "agent-x",
                "memory_key": "k",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "AUTH_INSUFFICIENT"


@pytest.mark.asyncio
async def test_auth_required_rejects_insufficient_role(monkeypatch) -> None:
    """VIEWER role can't call memory.set (which needs WRITER) → 403."""
    import server.main
    from server.settings import settings

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")

    from server.auth.jwt import mint_token

    token = mint_token(
        subject="reader",
        org="clickhouse",
        namespaces=["ns1"],
        roles=["VIEWER"],
    )

    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.main.app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/v1/memory.set",
            json={
                "org": "clickhouse",
                "namespace": "ns1",
                "agent": "agent",
                "memory_key": "k",
                "memory_type": "semantic",
                "value": 1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403, resp.text
