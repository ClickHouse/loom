"""End-to-end dispatch tests for /v1/admin/* endpoints.

These tests mount the admin router on a minimal FastAPI app and use
FastAPI's TestClient to hit each endpoint. The admin_repo functions
they call are monkey-patched to return canned data — no live
ClickHouse required.

The point is to cover the dispatch path that unit-testing the input
model and the repo function separately can't catch:
  - input model validation rejects bad payloads with 422
  - the handler builds the output dict in the right shape
  - the output model accepts that shape and returns 200 JSON
  - response shape matches what older SDK callers expect (key names
    + types unchanged)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.core.ch import current_query_tag
from server.insights.app.admin_routes import admin_router, org_admin_router
from server.insights.store import admin as admin_repo

# The overrides an admin route wrote to loom.runtime_overrides, in order.
persisted: list[tuple[str, tuple, object]] = []
# The query tag each of those writes ran under.
write_tags: list[str] = []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient over a minimal app mounting admin_router. Each test
    monkey-patches the admin_repo functions it exercises.

    Runs with auth disabled: the whole admin router is gated by a
    bearer+role dependency (see tests/unit/test_admin_authority.py for
    that contract); these tests cover the dispatch path behind the gate."""
    from server import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "auth_required", False)
    persisted.clear()
    write_tags.clear()

    async def put(kind: str, key: tuple, value: object = None) -> None:
        persisted.append((kind, key, value))
        write_tags.append(current_query_tag())

    async def clear(kind: str, key: tuple) -> None:
        persisted.append((kind, key, None))
        write_tags.append(current_query_tag())

    monkeypatch.setattr("server.core.runtime_overrides.put", put)
    monkeypatch.setattr("server.core.runtime_overrides.clear", clear)
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(org_admin_router)
    return TestClient(app)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_schema_status_returns_drift_shape(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake() -> list[str]:
        return ["memory", "audit_log"]

    monkeypatch.setattr(admin_repo, "list_loom_tables", fake)
    r = client.get("/v1/admin/schema.status")
    assert r.status_code == 200
    body = r.json()
    assert "expected" in body
    assert "present" in body
    assert "missing" in body
    assert "drift" in body


def test_audit_summary_default_window(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "event_type": "memory.set",
                "cnt": 42,
                "first_ts": datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
                "last_ts": datetime(2026, 5, 17, 12, 5, tzinfo=UTC),
            }
        ]

    monkeypatch.setattr(admin_repo, "audit_summary", fake)
    r = client.post("/v1/admin/audit.summary", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 42
    assert body["since_seconds"] == 86400
    assert len(body["rows"]) == 1
    assert body["rows"][0]["event_type"] == "memory.set"


def test_audit_summary_rejects_out_of_range_since(client: TestClient) -> None:
    """since_seconds bound is 90 days. A 100-day value must 422 at
    input-validation, not silently truncate."""
    r = client.post(
        "/v1/admin/audit.summary",
        json={"since_seconds": 100 * 86400},
    )
    assert r.status_code == 422


def test_events_tail_filters_thread_through(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake(
        since_seconds: int, limit: int, *, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        captured["since"] = since_seconds
        captured["limit"] = limit
        captured["filters"] = filters
        return []

    monkeypatch.setattr(admin_repo, "events_tail", fake)
    r = client.post(
        "/v1/admin/events.tail",
        json={
            "since_seconds": 120,
            "limit": 5,
            "org": "clickhouse",
            "agent": "bot",
            "success": False,
        },
    )
    assert r.status_code == 200
    assert captured["since"] == 120
    assert captured["limit"] == 5
    assert captured["filters"] == {"org": "clickhouse", "agent": "bot", "success": False}


def test_budget_set_install(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import budgets

    monkeypatch.setattr(
        budgets,
        "get_overrides",
        lambda: type("X", (), {"set": lambda s, op, ms: None, "clear": lambda s, op: None})(),
    )
    r = client.post(
        "/v1/admin/budget.set",
        json={"op": "memory.set", "budget_ms": 50},
    )
    assert r.status_code == 200
    body = r.json()
    # Pydantic v2 with default config emits the full output schema
    # (including the nullable `cleared` field as null). Older SDK
    # clients that ignore unknown nulls keep working; new clients see
    # the canonical shape advertised in the OpenAPI doc.
    assert body["ok"] is True
    assert body["op"] == "memory.set"
    assert persisted == [("budget.op", ("memory.set",), 50)]
    assert body["budget_ms"] == 50
    assert body.get("cleared") in (None, False)


def test_budget_set_clear_omits_budget_ms(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import budgets

    monkeypatch.setattr(
        budgets,
        "get_overrides",
        lambda: type("X", (), {"set": lambda s, op, ms: None, "clear": lambda s, op: None})(),
    )
    r = client.post(
        "/v1/admin/budget.set",
        json={"op": "memory.set", "clear": True},
    )
    assert r.status_code == 200
    assert r.json()["cleared"] is True
    assert persisted == [("budget.op", ("memory.set",), None)]


def test_budget_set_missing_budget_ms_when_not_clear(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler explicitly raises 422 if budget_ms is missing and
    clear=false. Pydantic accepts the request (clear default false,
    budget_ms optional); the explicit check inside the handler
    catches it."""
    from server.observability import budgets

    monkeypatch.setattr(
        budgets,
        "get_overrides",
        lambda: type("X", (), {"set": lambda s, op, ms: None, "clear": lambda s, op: None})(),
    )
    r = client.post(
        "/v1/admin/budget.set",
        json={"op": "memory.set"},
    )
    assert r.status_code == 422
    assert "budget_ms required" in r.text


def test_ratelimit_set_exactly_one_of_ns_or_org(client: TestClient) -> None:
    # Neither set → 422.
    r = client.post(
        "/v1/admin/ratelimit.set",
        json={"op_class": "memory", "capacity": 10, "rate": 1},
    )
    assert r.status_code == 422
    # Both set → 422.
    r2 = client.post(
        "/v1/admin/ratelimit.set",
        json={
            "namespace": "ns",
            "org": "clickhouse",
            "op_class": "memory",
            "capacity": 10,
            "rate": 1,
        },
    )
    assert r2.status_code == 422


def test_fleet_cost_group_by_literal_validation(client: TestClient) -> None:
    """group_by is a Literal['namespace', 'agent', 'org']; an invalid
    value 422s at input validation."""
    r = client.post(
        "/v1/admin/fleet.cost",
        json={"group_by": "user"},
    )
    assert r.status_code == 422


def test_token_mint_minimum_namespaces(client: TestClient) -> None:
    """namespaces must have at least one entry."""
    r = client.post(
        "/v1/admin/token.mint",
        json={"name": "x", "org": "y", "namespaces": []},
    )
    assert r.status_code == 422


def test_token_mint_happy_path_when_auth_not_required(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LOOM_AUTH_REQUIRED=false, anyone can mint (dev mode), and the token
    is saved to the token list like any other."""
    from server import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "auth_required", False)
    from server.auth import jwt as jwt_mod

    saved: list[dict] = []

    class _CH:
        async def query_one(self, sql: str, params: object = None) -> None:
            return None

        async def insert(self, table: str, rows: list[dict], **kw: object) -> None:
            saved.extend(rows)

    monkeypatch.setattr(jwt_mod, "mint_token", lambda **kwargs: "jwt-x")

    async def no_restricted(*a: object) -> list[str]:
        return []

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr("server.identity.app.token_grants.grant_restricted_to_token", no_restricted)
    r = client.post(
        "/v1/admin/token.mint",
        json={
            "name": "alice-bot",
            "org": "clickhouse",
            "namespaces": ["personal-alice"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] == "jwt-x"
    assert body["roles"] == ["writer"] and body["ttl_seconds"] == 90 * 86400
    assert body["subject"].startswith("sa:alice-bot-")
    assert [row["sa_subject"] for row in saved] == [body["subject"]]


def test_customer_list_endpoint_removed(client: TestClient) -> None:
    """customer_id admin surface is removed from the active API."""
    r = client.post(
        "/v1/admin/customer.list",
        json={"org": "clickhouse"},  # missing namespace
    )
    assert r.status_code == 404


def test_namespace_adopt_stays_inside_the_admins_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """An org's admin adopts orphaned namespaces of that org only, as token.mint mints only
    there: a grant written into another org would make the caller its admin."""
    import asyncio

    from fastapi import HTTPException

    from server.auth.jwt import AuthInfo
    from server.insights.app import admin_models
    from server.insights.app import admin_routes as admin
    from server.operations.base import Role

    async def other_org_admin(_request: object) -> AuthInfo:
        return AuthInfo(
            subject="eve",
            org="acme",
            namespaces=("*",),
            roles=(Role.ORG_ADMIN,),
            is_service_account=False,
        )

    def no_client() -> object:
        raise AssertionError("refused before any read or write")

    monkeypatch.setattr(admin, "require_org_admin", other_org_admin)
    monkeypatch.setattr("server.core.ch.get_client", no_client)
    inp = admin_models.NamespaceAdoptInput(org="globex", namespace="ops", new_admin="eve")
    with pytest.raises(HTTPException) as err:
        asyncio.run(admin.admin_namespace_adopt(object(), inp))  # type: ignore[arg-type]
    assert err.value.status_code == 403


def test_ratelimit_set_writes_the_override_through_and_applies_it_here(
    client: TestClient,
) -> None:
    """The override reaches every process through loom.runtime_overrides; this
    process applies it at once."""
    from server.core.ratelimit import Limit, get_limiter

    body = {"namespace": "ns-x", "op_class": "memory", "capacity": 3, "rate": 1}
    assert client.post("/v1/admin/ratelimit.set", json=body).status_code == 200
    assert persisted == [("ratelimit.namespace", ("ns-x", "memory"), {"capacity": 3, "rate": 1})]
    assert get_limiter()._ns_overrides[("ns-x", "memory")] == Limit(3, 1)
    clear = {"namespace": "ns-x", "op_class": "memory", "clear": True}
    assert client.post("/v1/admin/ratelimit.set", json=clear).status_code == 200
    assert persisted[-1] == ("ratelimit.namespace", ("ns-x", "memory"), None)
    assert ("ns-x", "memory") not in get_limiter()._ns_overrides


def test_an_invalid_value_is_refused_before_it_is_stored(client: TestClient) -> None:
    """A stored override binds every process and outlives restarts."""
    r = client.post("/v1/admin/budget.set", json={"op": "memory.set", "budget_ms": -5})
    assert r.status_code == 422
    for bad in ({"capacity": 0, "rate": 1}, {"capacity": 5, "rate": -1}):
        body = {"namespace": "ns", "op_class": "memory", **bad}
        assert client.post("/v1/admin/ratelimit.set", json=body).status_code == 422
    assert persisted == []


def test_a_refused_override_write_is_a_typed_409_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only ClickHouse user: the route answers OVERRIDE_NOT_STORED, not a bare 500,
    and this process applies nothing."""
    from server import settings as settings_mod
    from server.core import runtime_overrides as ro
    from server.core.errors import register_exception_handler
    from server.core.ratelimit import get_limiter
    from server.observability.budgets import get_overrides

    class _ReadOnly:
        async def query_one(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            return {"v": 1}

        async def insert(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("Code: 164. DB::Exception: readonly mode")

    monkeypatch.setattr(settings_mod.settings, "auth_required", False)
    monkeypatch.setattr(ro, "_default_client", lambda: _ReadOnly())
    app = FastAPI()
    app.include_router(admin_router)
    register_exception_handler(app)
    client = TestClient(app)
    body = {"namespace": "ns-ro", "op_class": "memory", "capacity": 3, "rate": 1}
    r = client.post("/v1/admin/ratelimit.set", json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OVERRIDE_NOT_STORED"
    assert ("ns-ro", "memory") not in get_limiter()._ns_overrides
    r = client.post("/v1/admin/budget.set", json={"op": "memory.ro", "budget_ms": 5})
    assert r.status_code == 409 and get_overrides().get("memory.ro") is None


def test_override_writes_are_tagged_with_their_route(client: TestClient) -> None:
    body = {"namespace": "ns-x", "op_class": "memory", "capacity": 3, "rate": 1}
    assert client.post("/v1/admin/ratelimit.set", json=body).status_code == 200
    assert (
        client.post("/v1/admin/budget.set", json={"op": "memory.get", "clear": True}).status_code
        == 200
    )
    assert write_tags == ["admin.ratelimit_set", "admin.budget_set"]
