"""Transport + admin-surface security contracts: admin role gate, id-addressed
ops authorizing against the stored row's org/namespace, and rate limiting
running after auth. Repositories monkey-patched; tokens are real JWTs."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.auth.jwt import AuthInfo, mint_token  # noqa: E402
from server.operations.base import Role  # noqa: E402
from server.settings import settings  # noqa: E402


def _mint(roles: list[str], org: str = "dev") -> str:
    return mint_token(
        subject=f"test-{'-'.join(roles)}@{org}",
        org=org,
        namespaces=["*"],
        roles=roles,
        ttl_seconds=300,
        is_service_account=True,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Admin router gate ---


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", True)
    from server.insights.app.admin_routes import admin_router

    app = FastAPI()
    app.include_router(admin_router)
    return TestClient(app)


def test_every_admin_route_401s_without_bearer(admin_client: TestClient) -> None:
    """Gate runs before validation on every registered route, GET and POST."""
    from server.insights.app.admin_routes import admin_router

    routes = [r for r in admin_router.routes if hasattr(r, "methods")]
    assert routes, "admin router has no routes?"
    for route in routes:
        for method in route.methods:
            if method == "GET":
                r = admin_client.get(route.path)
            else:
                r = admin_client.request(method, route.path, json={})
            assert r.status_code == 401, f"{method} {route.path} -> {r.status_code}"
            assert r.json()["detail"]["code"] == "AUTH_INVALID"


def test_admin_route_401_with_invalid_bearer(admin_client: TestClient) -> None:
    r = admin_client.post("/v1/admin/fleet.cost", json={}, headers=_bearer("not-a-jwt"))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "AUTH_INVALID"


def test_admin_route_403_with_non_admin_bearer(admin_client: TestClient) -> None:
    """Writer token authenticates but lacks ADMIN."""
    r = admin_client.post(
        "/v1/admin/budget.set",
        json={"op": "memory.set", "budget_ms": 10},
        headers=_bearer(_mint(["writer"])),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"


def test_admin_route_allows_admin_bearer(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.insights.store import admin as admin_repo

    async def fake(
        since_seconds: int, limit: int, *, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(admin_repo, "events_tail", fake)
    r = admin_client.post(
        "/v1/admin/events.tail", json={}, headers=_bearer(_mint(["platform_admin"]))
    )
    assert r.status_code == 200, r.text


# --- MCP OAuth challenge: 401 + WWW-Authenticate so a native connector
#     discovers the AS and starts browser sign-in ---


def _mcp_client(monkeypatch: pytest.MonkeyPatch, *, oauth: bool) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "oauth_enabled", oauth)
    from server.transport import mcp as mcp_mod

    app = FastAPI()
    mcp_mod.attach(app)
    return TestClient(app)


def test_mcp_unauth_initialize_emits_oauth_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oauth_enabled + no bearer → 401 with a resource_metadata pointer."""
    c = _mcp_client(monkeypatch, oauth=True)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert r.status_code == 401, r.text
    wa = r.headers.get("WWW-Authenticate", "")
    assert wa.startswith("Bearer ") and 'resource_metadata="' in wa
    assert "/.well-known/oauth-protected-resource" in wa


def test_mcp_unauth_is_401_without_oauth_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth off → still 401 + `WWW-Authenticate: Bearer realm="loom"`. A
    tolerant 200 with an `id: null` error frame is rejected by the reference
    SDK at schema parse, so clients would show a zod error instead of "needs
    authentication". The frame's id echoes the request."""
    c = _mcp_client(monkeypatch, oauth=False)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert r.status_code == 401, r.text
    assert r.headers.get("WWW-Authenticate", "").startswith('Bearer realm="loom"')
    assert r.json()["id"] == 1
    assert "bearer" in r.json()["error"]["message"].lower()


# --- Id-addressed ops authorize against the stored row ---


def _auth_info(org: str, *, roles: tuple[Role, ...] = (Role.WRITER,)) -> AuthInfo:
    return AuthInfo(
        subject=f"sa:test@{org}",
        org=org,
        namespaces=("*",),
        roles=roles,
        is_service_account=True,
    )


@pytest.fixture
def _no_grants(monkeypatch: pytest.MonkeyPatch):
    """Pin grant fallback to 'none' so only JWT claims drive the check."""
    from server.auth import authz as authz_mod

    async def none(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(authz_mod, "_lookup_grant", none)


@pytest.fixture
def _auth_ctx():
    """Set/clear the per-request auth ContextVar around a test."""
    from server.auth.context import set_current_auth

    def _set(auth: AuthInfo | None) -> None:
        set_current_auth(auth)

    yield _set
    set_current_auth(None)


def _erasure_row(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC).replace(tzinfo=None)
    row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "org": "dev",
        "scope_type": "user_id",
        "scope_value": "user-123",
        "requester": "ops@dev",
        "status": "completed",
        "current_step": 9,
        "failure_reason": "",
        "requested_at": now,
        "completed_at": now,
    }
    row.update(overrides)
    return row


async def test_erasure_status_foreign_org_token_is_not_found(
    monkeypatch: pytest.MonkeyPatch, _no_grants, _auth_ctx
) -> None:
    from server.erasure.api.erasure import ErasureStatusInput
    from server.erasure.app.handlers import handle_erasure_status
    from server.erasure.store import ledger as erasure_ledger

    row = _erasure_row()

    async def fake_fetch(erasure_id: str, **kwargs: Any) -> dict[str, Any]:
        return dict(row)

    monkeypatch.setattr(erasure_ledger, "fetch_request_status", fake_fetch)

    _auth_ctx(_auth_info("other-org", roles=(Role.ADMIN,)))
    out = await handle_erasure_status(ErasureStatusInput(erasure_id=row["id"]))
    # Hidden like an unknown id; nothing of the saga (its org first) comes back.
    assert out.found is False and out.org is None and out.erasure_id == row["id"]


async def test_erasure_status_namespace_scope_requires_namespace_coverage(
    monkeypatch: pytest.MonkeyPatch, _no_grants, _auth_ctx
) -> None:
    from server.erasure.api.erasure import ErasureStatusInput
    from server.erasure.app.handlers import handle_erasure_status
    from server.erasure.store import ledger as erasure_ledger

    row = _erasure_row(scope_type="namespace", scope_value="ns-secret")

    async def fake_fetch(erasure_id: str, **kwargs: Any) -> dict[str, Any]:
        return dict(row)

    monkeypatch.setattr(erasure_ledger, "fetch_request_status", fake_fetch)

    # Same org, but token namespace claim doesn't cover the scope.
    narrow = AuthInfo(
        subject="sa:narrow@dev",
        org="dev",
        namespaces=("ns-other",),
        roles=(Role.VIEWER,),
        is_service_account=True,
    )
    _auth_ctx(narrow)
    out = await handle_erasure_status(ErasureStatusInput(erasure_id=row["id"]))
    assert out.found is False and out.scope_value is None


async def test_erasure_status_same_org_token_allowed(
    monkeypatch: pytest.MonkeyPatch, _no_grants, _auth_ctx
) -> None:
    from server.erasure.api.erasure import ErasureStatusInput
    from server.erasure.app.handlers import handle_erasure_status
    from server.erasure.store import ledger as erasure_ledger

    row = _erasure_row()

    async def fake_fetch(erasure_id: str, **kwargs: Any) -> dict[str, Any]:
        return dict(row)

    monkeypatch.setattr(erasure_ledger, "fetch_request_status", fake_fetch)

    _auth_ctx(_auth_info("dev", roles=(Role.VIEWER,)))
    out = await handle_erasure_status(ErasureStatusInput(erasure_id=row["id"]))
    assert out.found is True
    assert out.org == "dev"


# --- Rate limiting runs after auth (anonymous 401s don't drain buckets) ---


@pytest.fixture
def transport_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", True)
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    return TestClient(app)


def test_anonymous_401s_do_not_drain_the_namespace_bucket(
    transport_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.ratelimit import Limit, get_limiter
    from server.memory.store import blacklist as blacklist_repo

    ns = f"rl-sec-{uuid.uuid4().hex[:8]}"
    limiter = get_limiter()
    # Capacity 2, effectively no refill within the test's lifetime.
    limiter.set_namespace_limit(ns, "default", Limit(capacity=2, rate=1e-6))

    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(blacklist_repo, "list_for_scope", fake_list)
    body = {"org": "dev", "namespace": ns}
    try:
        # Anonymous flood: every request 401s, draining nothing.
        for _ in range(4):
            r = transport_client.post("/v1/blacklist.list", json=body)
            assert r.status_code == 401, r.text

        # Legitimate caller still has the full capacity-2 burst.
        headers = _bearer(_mint(["writer"], org="dev"))
        assert (
            transport_client.post("/v1/blacklist.list", json=body, headers=headers).status_code
            == 200
        )
        assert (
            transport_client.post("/v1/blacklist.list", json=body, headers=headers).status_code
            == 200
        )
        # Limiter still fires once the bucket is spent.
        r = transport_client.post("/v1/blacklist.list", json=body, headers=headers)
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "RATE_LIMITED"
    finally:
        limiter.clear_namespace_limit(ns, "default")
        limiter.reset()


def test_org_bucket_keys_on_verified_token_org(
    transport_client: TestClient, monkeypatch: pytest.MonkeyPatch, _no_grants
) -> None:
    """A token from org B naming org A in the body must not consume org A's
    org-tier bucket — the verified token org wins over body input."""
    from server.core.ratelimit import Limit, get_limiter
    from server.memory.store import blacklist as blacklist_repo

    ns = f"rl-org-{uuid.uuid4().hex[:8]}"
    victim_org = f"victim-{uuid.uuid4().hex[:8]}"
    limiter = get_limiter()
    limiter.set_org_limit(victim_org, "default", Limit(capacity=1, rate=1e-6))

    async def fake_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(blacklist_repo, "list_for_scope", fake_list)
    try:
        # Org-B caller naming victim_org in the body; org tier charges the
        # token org, so the victim bucket stays untouched. (Authz 403s the
        # cross-org body, but only after the limiter ran — what we pin here.)
        attacker_headers = _bearer(_mint(["writer"], org="attacker-org"))
        body = {"org": victim_org, "namespace": ns}
        for _ in range(3):
            r = transport_client.post("/v1/blacklist.list", json=body, headers=attacker_headers)
            assert r.status_code == 403, r.text

        # Victim org's single token is still available.
        victim_headers = _bearer(_mint(["writer"], org=victim_org))
        r = transport_client.post("/v1/blacklist.list", json=body, headers=victim_headers)
        assert r.status_code == 200, r.text
    finally:
        limiter.clear_org_limit(victim_org, "default")
        limiter.reset()


@pytest.mark.parametrize(
    ("op", "body", "raw"),
    [
        # A body the op's model refuses: a named 422 for a caller with a token.
        ("blacklist.list", {"org": "dev"}, None),
        # A misspelled argument: the write's named refusal.
        ("memory.set", {"org": "dev", "namespace": "n", "memory_key": "k", "valeu": "x"}, None),
        # Not JSON at all.
        ("blacklist.list", None, b"{not json"),
        # A code.* body that fails validation also schedules a code trace row.
        ("code.search", {"query": 7, "top_k": "many"}, None),
    ],
)
def test_an_anonymous_request_401s_before_its_body_is_read(
    transport_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    op: str,
    body: dict[str, Any] | None,
    raw: bytes | None,
) -> None:
    """A deployment that requires a token answers an anonymous request with
    the 401 whatever its body says: no field errors for a caller it does not
    know, and no ledger or code-trace row written on its behalf."""
    from server.transport import http as http_mod

    rows: list[str] = []
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: rows.append(k["error_code"]))
    monkeypatch.setattr(
        http_mod,
        "schedule_code_trace_validation_failure",
        lambda *a, **k: rows.append("code_trace"),
    )
    if raw is not None:
        r = transport_client.post(
            f"/v1/{op}", content=raw, headers={"Content-Type": "application/json"}
        )
    else:
        r = transport_client.post(f"/v1/{op}", json=body)
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["code"] == "AUTH_INVALID"
    assert rows == []


def test_a_verified_caller_still_gets_the_named_validation_refusal(
    transport_client: TestClient,
) -> None:
    r = transport_client.post(
        "/v1/blacklist.list", json={"org": "dev"}, headers=_bearer(_mint(["writer"]))
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "VALIDATION_FAILED"
