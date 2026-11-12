"""A cell serves only the orgs it holds: the tenancy check (server/auth/tenancy.py) in
the op gate, the routes outside the registry and the request middleware, and a census
of every route (docs/architecture/cells.md, "The tenancy check and placement")."""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import sys
import time
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from server.auth import tenancy
from server.auth.jwt import AuthInfo
from server.core import cell, errors
from server.operations.base import Role
from server.settings import settings


def _auth(org: str, *roles: Role) -> AuthInfo:
    return AuthInfo(
        subject="alice",
        org=org,
        namespaces=("ns",),
        roles=roles or (Role.WRITER,),
        is_service_account=False,
    )


class _PG:
    """The cell's Postgres as the check sees it: which orgs it holds, and its mode."""

    def __init__(self, held: set[str], mode: str = "enforced") -> None:
        self.held = set(held)
        self.mode = mode
        self.writes: list[str] = []
        self.down = False
        # Another process records this org between our read and our insert.
        self.racer: str | None = None

    async def fetchval(self, sql: str, org: str) -> Any:
        if self.down:
            raise ConnectionError("postgres is down")
        if sql == tenancy._HOLDS:
            return 1 if org in self.held else None
        if sql == tenancy._RECORD:
            if self.mode != "recording":
                return None
            if org == self.racer:
                self.held.add(org)  # the other process's row: ours conflicts
                return None
            self.held.add(org)
            self.writes.append(org)
            return 1
        raise AssertionError(sql)

    async def execute(self, sql: str, org: str) -> str:
        assert sql == tenancy._ADD
        self.held.add(org)
        self.writes.append(org)
        return "INSERT 0 1"


@pytest.fixture
def cell_pg(monkeypatch: pytest.MonkeyPatch) -> _PG:
    from server.core import pg

    fake = _PG({"held"})
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "cell_router_url", "https://router.test")
    monkeypatch.setattr(settings, "cell_router_token", "lcr_" + "0" * 128)
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})
    monkeypatch.setattr(tenancy, "_not_held", {})
    monkeypatch.setattr(tenancy, "_placing", {})
    # The directory answers that this first cell is `attaching` (its own tests below).
    monkeypatch.setattr(tenancy, "directory_says_attaching", AsyncMock(return_value=True))
    monkeypatch.setattr(cell, "_deployment_id", "dep-eu-1")
    monkeypatch.setattr(cell, "mode", lambda: fake.mode)
    monkeypatch.setattr(pg, "fetchval", fake.fetchval)
    monkeypatch.setattr(pg, "execute", fake.execute)
    return fake


def _router(
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> list[dict]:
    """The router's placement API answering `answer`; the calls it got."""
    calls: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "url": str(request.url),
                "body": json.loads(request.content),
                "auth": request.headers.get("authorization"),
            }
        )
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(status, json=answer, headers=headers)

    real = httpx.AsyncClient

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_handler), **kw)
    )
    return calls


def test_nothing_is_checked_without_loom_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg

    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(pg, "fetchval", AsyncMock(side_effect=AssertionError("read")))
    asyncio.run(tenancy.ensure_held("anything", _auth("anything")))


@pytest.mark.parametrize("org", [None, "", "-", "*"])
def test_an_org_free_call_is_not_checked(cell_pg: _PG, org: str | None) -> None:
    cell_pg.down = True  # nothing is read
    asyncio.run(tenancy.ensure_held(org, _auth("*")))


def test_a_held_org_passes_and_is_remembered(cell_pg: _PG) -> None:
    asyncio.run(tenancy.ensure_held("held", _auth("held")))
    cell_pg.down = True  # a held org is never wrong to serve: no read again
    asyncio.run(tenancy.ensure_held("held", _auth("held")))
    assert cell_pg.writes == []


def test_recording_records_a_new_org(cell_pg: _PG) -> None:
    cell_pg.mode = "recording"
    asyncio.run(tenancy.ensure_held("new", _auth("*", Role.PLATFORM_ADMIN)))
    assert cell_pg.writes == ["new"]


def test_enforced_places_a_scoped_principals_org_here(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _router(monkeypatch, {"cell": "eu-1", "deployment_id": "dep-eu-1"})
    asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert cell_pg.writes == ["new"]
    (call,) = calls
    assert call["url"] == "https://router.test/cells/v1/placements"
    assert call["auth"] == "Bearer lcr_" + "0" * 128
    assert call["body"]["org"] == "new" and call["body"]["cell"] == "eu-1"
    assert call["body"]["fingerprint"]["v"] == cell.FINGERPRINT_VERSION


def test_a_stale_recording_cache_goes_on_to_placement_once_postgres_is_enforced(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode cache still reads `recording` after attach committed `enforced`: the
    recording insert writes nothing, and the request is placed as in `enforced`; a
    principal not scoped to the org is refused with no router call."""
    monkeypatch.setattr(cell, "mode", lambda: "recording")
    calls = _router(monkeypatch, {"cell": "eu-1", "deployment_id": "dep-eu-1"})
    asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert cell_pg.writes == ["new"] and len(calls) == 1
    with pytest.raises(errors.WrongCellError):
        asyncio.run(tenancy.ensure_held("other", _auth("*", Role.PLATFORM_ADMIN)))
    assert len(calls) == 1 and "other" not in cell_pg.writes


def test_an_org_placed_elsewhere_answers_wrong_cell_and_writes_nothing(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    _router(monkeypatch, {"cell": "us-1", "deployment_id": "dep-us-1"})
    with pytest.raises(errors.WrongCellError) as raised:
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert raised.value.status_code == 409 and raised.value.code == "WRONG_CELL"
    assert cell_pg.writes == []


@pytest.mark.parametrize("principal", ["*", "other"])
def test_a_principal_not_scoped_to_the_org_never_places_it(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch, principal: str
) -> None:
    calls = _router(monkeypatch, {"cell": "eu-1", "deployment_id": "dep-eu-1"})
    with pytest.raises(errors.WrongCellError):
        asyncio.run(tenancy.ensure_held("new", _auth(principal, Role.PLATFORM_ADMIN)))
    with pytest.raises(errors.WrongCellError):
        asyncio.run(tenancy.ensure_held("new", None))
    assert calls == [] and cell_pg.writes == []


@pytest.mark.parametrize(
    "answer,status",
    [
        (httpx.ConnectError("refused"), 200),
        ({"detail": "closed"}, 503),
        ({"cell": ""}, 200),
        ("not an object", 200),
    ],
)
def test_a_failed_placement_answers_503_and_writes_nothing(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch, answer: Any, status: int
) -> None:
    _router(monkeypatch, answer, status)
    with pytest.raises(errors.BackendUnavailableError):
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert cell_pg.writes == []


@pytest.mark.parametrize(
    "url,token,missing",
    [
        ("", "lcr_" + "0" * 128, "LOOM_CELL_ROUTER_URL"),
        ("http://router", "", "LOOM_CELL_ROUTER_TOKEN"),
    ],
)
def test_no_router_configured_answers_503(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch, url: str, token: str, missing: str
) -> None:
    monkeypatch.setattr(settings, "cell_router_url", url)
    monkeypatch.setattr(settings, "cell_router_token", token)
    with pytest.raises(errors.BackendUnavailableError, match=missing):
        asyncio.run(tenancy.ensure_held("new", _auth("new")))


def test_an_unreadable_tenancy_answers_503(cell_pg: _PG) -> None:
    cell_pg.down = True
    with pytest.raises(errors.BackendUnavailableError) as raised:
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert "ConnectionError" in raised.value.message  # its kind only, never its text


def test_the_op_gate_checks_tenancy_before_anything_it_provisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates

    order: list[str] = []

    async def _held(org: Any, auth: Any) -> None:
        order.append(f"tenancy:{org}")
        raise errors.WrongCellError(str(org))

    async def _acted(*_a: Any) -> None:
        order.append("acted_for")

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    monkeypatch.setattr("server.auth.authz.check_acted_for", _acted)
    op = OPERATIONS_BY_NAME["memory.set"]
    target = gates.Target(org="acme", namespace="ns", named_namespace="ns", register_namespace="ns")
    with pytest.raises(errors.WrongCellError):
        asyncio.run(gates.authorize(op, _auth("acme"), target, transport="http"))
    assert order == ["tenancy:acme"]


def test_the_raw_routes_authorize_through_the_tenancy_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz

    seen: list[str] = []

    async def _held(org: Any, auth: Any) -> None:
        seen.append(str(org))
        raise errors.WrongCellError(str(org))

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    with pytest.raises(errors.WrongCellError):
        asyncio.run(authz.check_authorized(_auth("acme"), Role.VIEWER, org="acme", namespace="ns"))
    assert seen == ["acme"]


def test_admit_org_refuses_a_foreign_org_before_the_tenancy_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from server.insights.app.admin_routes import admit_org

    checked: list[str] = []

    async def _held(org: Any, auth: Any) -> None:
        checked.append(str(org))

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    with pytest.raises(HTTPException):
        asyncio.run(admit_org(_auth("acme", Role.ORG_ADMIN), "other", "mint a token for"))
    asyncio.run(admit_org(_auth("acme", Role.ORG_ADMIN), "acme", "mint a token for"))
    assert checked == ["acme"]


@pytest.mark.asyncio
async def test_the_middleware_checks_a_raw_routes_token_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import tenancy_gate

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr("server.auth.jwt.verify_token", lambda _t: _auth("other"))

    async def _held(org: Any, auth: Any) -> None:
        if org != "held":
            raise errors.WrongCellError(str(org), cell="us-1")

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    headers = [(b"authorization", b"Bearer x")]
    for path, reached in (
        ("/v1/me/sharing-summary", False),
        ("/v1/memory.set", True),
        ("/mcp", True),
    ):
        app, send = AsyncMock(), AsyncMock()
        scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
        await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), send)
        assert app.await_count == int(reached), path
        if not reached:
            assert send.await_args_list[0].args[0]["status"] == 409
            assert b"WRONG_CELL" in send.await_args_list[1].args[0]["body"]


# ---- census --------------------------------------------------------------------

# Every route outside the op registry, and how the org it acts in is checked:
#   "token": it acts only in the verified token's own org, which the middleware checks;
#   "named": its request names the org, and its module checks that org itself
#            (check_authorized, admit_org or tenancy.ensure_held);
#   anything else: why it acts in no org.
_ORG_FREE_MODULES = {
    "server.identity.app.dashboard_routes": "the dashboard's pages and assets: no data",
    "fastapi.applications": "the API docs pages",
}
ROUTES: dict[str, str] = {
    "/.well-known/oauth-authorization-server GET": "discovery document: no data",
    "/assets WS": "the dashboard's static assets",
    "/mcp DELETE": "ends an MCP session: no org's data",
    "/mcp POST": "op transport: the op gate checks each call's org",
    "/oauth/agent-claim GET": "the claim page: no data",
    "/oauth/agent-claim POST": "named",
    "/oauth/callback GET": "sign-in: JIT writes an account only in an org the cell holds (`jit._provision_jit`)",
    "/oauth/device GET": "the device page: no data",
    "/oauth/device POST": "device sign-in: the account is this cell's",
    "/oauth/device/approve POST": "approves a device code by its ticket and cookie: the account is this cell's",
    "/oauth/device_authorization POST": "starts a device flow: no data",
    "/oauth/link POST": "links this cell's account by its ticket and cookie",
    "/oauth/revoke POST": "revokes this cell's own refresh token or JWT",
    "/oauth/set-password GET": "the page: no data",
    "/oauth/set-password POST": "a set-password link of this cell's account",
    "/oauth/sso GET": "starts sign-in: no data",
    "/oauth/token POST": "token exchange: the account or principal is this cell's",
    "/scim/v2/Groups GET": "token",
    "/scim/v2/Groups POST": "token",
    "/scim/v2/Groups/{group_id} DELETE": "token",
    "/scim/v2/Groups/{group_id} GET": "token",
    "/scim/v2/Groups/{group_id} PATCH": "token",
    "/scim/v2/Groups/{group_id} PUT": "token",
    "/v1/.well-known/jwks.json GET": "this cell's public keys",
    "/v1/.well-known/openid-configuration GET": "discovery document: no data",
    "/v1/admin/audit.summary POST": "a platform admin's read: an org the cell does not hold has no rows",
    "/v1/admin/bots.create POST": "named",
    "/v1/admin/bots.disable POST": "a bot of this cell, by name",
    "/v1/admin/bots.list POST": "lists this cell's bots",
    "/v1/admin/bots.provision_agent POST": "a bot of this cell, by name",
    "/v1/admin/budget.set POST": "a per-cell budget setting row, not tenant data",
    "/v1/admin/budget.violations POST": "a platform admin's read: an org the cell does not hold has no rows",
    "/v1/admin/claim_release POST": "named",
    "/v1/admin/consumers POST": "a platform admin's read: an org the cell does not hold has no rows",
    "/v1/admin/events.tail POST": "a platform admin's read: an org the cell does not hold has no rows",
    "/v1/admin/fleet.cost POST": "the cell's own fleet cost",
    "/v1/admin/models GET": "the cell's model configuration",
    "/v1/admin/models POST": "the cell's model configuration",
    "/v1/admin/models/reset POST": "the cell's model configuration",
    "/v1/admin/models/retry POST": "the cell's model configuration",
    "/v1/admin/models/validate POST": "the cell's model configuration",
    "/v1/admin/namespace.adopt POST": "named",
    "/v1/admin/prompts.evaluate POST": "evaluates prompts: no org's data",
    "/v1/admin/ratelimit.set POST": "named",
    "/v1/admin/revocations GET": "lists this cell's revocations",
    "/v1/admin/revoke_subject POST": "named",
    "/v1/admin/schema.status GET": "the cell's schema",
    "/v1/admin/settings.history POST": "the cell's settings",
    "/v1/admin/settings.list POST": "the cell's settings",
    "/v1/admin/settings.set POST": "the cell's settings",
    "/v1/admin/sso.adopt POST": "named",
    "/v1/admin/sso.link POST": "named",
    "/v1/admin/sso.password_link POST": "named",
    "/v1/admin/sso.unlink POST": "named",
    "/v1/admin/token.mint POST": "named",
    "/v1/admin/workers.status GET": "the cell's workers",
    "/v1/api/openapi.json GET": "the API description",
    "/v1/assistant/chat/completions POST": "token",
    "/v1/auth/agent-signup POST": "named",
    "/v1/auth/login POST": "password sign-in: the account is this cell's",
    "/v1/auth/self-signup POST": "named",
    "/v1/health GET": "health probe",
    "/v1/health/live GET": "health probe",
    "/v1/health/ready GET": "health probe",
    "/v1/health/deployment GET": "health probe",
    "/v1/knowledge.download GET": "named",
    "/v1/knowledge.upload POST": "named",
    "/v1/me/service_accounts GET": "token",
    "/v1/me/service_accounts.create POST": "token",
    "/v1/me/service_accounts.revoke POST": "token",
    "/v1/me/set-default-namespace POST": "token",
    "/v1/me/sharing-summary GET": "token",
    "/v1/operations GET": "the op catalogue",
    "/v1/task/watch GET": "named",
    "/v1/whoami GET": "describes the caller's own token: no org's data",
}
# SCIM's user routes, mounted only with SCIM on: the accounts are this cell's, in the
# org it was attached with (`oidc_jit_org`, placed at attach).
SCIM_USERS = "server.auth.scim_routes"
_CHECKS = {"check_authorized", "admit_org", "ensure_held"}


def _routes(routes: list[Any], prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for r in routes:
        included = getattr(r, "original_router", None)
        if included is not None:
            out += _routes(included.routes, prefix + getattr(r, "path", ""))
            continue
        sub = getattr(r, "routes", None)
        if sub and not getattr(r, "endpoint", None):
            out += _routes(sub, prefix + getattr(r, "path", ""))
            continue
        methods = ",".join(sorted(getattr(r, "methods", None) or ["WS"]))
        module = getattr(getattr(r, "endpoint", None), "__module__", "?")
        out.append((f"{prefix}{getattr(r, 'path', '?')} {methods}", module))
    return out


def _endpoints(routes: list[Any], prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for r in routes:
        included = getattr(r, "original_router", None)
        if included is not None:
            out += _endpoints(included.routes, prefix + getattr(r, "path", ""))
            continue
        sub = getattr(r, "routes", None)
        if sub and not getattr(r, "endpoint", None):
            out += _endpoints(sub, prefix + getattr(r, "path", ""))
            continue
        methods = ",".join(sorted(getattr(r, "methods", None) or ["WS"]))
        out.append((f"{prefix}{getattr(r, 'path', '?')} {methods}", getattr(r, "endpoint", None)))
    return out


def _calls(fn: Any) -> set[str]:
    """The names a function calls (`f(...)` and `x.f(...)`), from its syntax tree, so a
    name in a comment or docstring does not count."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                out.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                out.add(n.func.attr)
    return out


def _checks(endpoint: Any) -> bool:
    """Whether a route's handler, or a function of its module it calls, calls a check."""
    called = _calls(endpoint)
    if called & _CHECKS:
        return True
    module = sys.modules[endpoint.__module__]
    for name in called:
        fn = getattr(module, name, None)
        if callable(fn) and getattr(fn, "__module__", "") == module.__name__:
            if _calls(fn) & _CHECKS:
                return True
    return False


def test_every_route_is_tenancy_checked_or_org_free() -> None:
    from server.main import app
    from server.operations.registry import ALL_OPERATIONS

    op_paths = {f"{op.http_path} POST" for op in ALL_OPERATIONS}
    raw = {
        key: module
        for key, module in _routes(app.routes)
        if key not in op_paths and module not in _ORG_FREE_MODULES
    }
    unlisted = sorted(set(raw) - set(ROUTES))
    gone = sorted(set(ROUTES) - set(raw))
    assert not unlisted, f"classify these routes in ROUTES: {unlisted}"
    assert not gone, f"these ROUTES entries name no route: {gone}"
    from server.transport.tenancy_gate import acts_in_token_org

    # The middleware checks exactly the routes classified `token`.
    drift = sorted(
        key for key in ROUTES if acts_in_token_org(key.split(" ")[0]) != (ROUTES[key] == "token")
    )
    assert not drift, f"the middleware and the census disagree on: {drift}"
    endpoints = {key: ep for key, ep in _endpoints(app.routes)}
    missing = [key for key, how in ROUTES.items() if how == "named" and not _checks(endpoints[key])]
    assert not missing, f"these routes name an org but check none: {missing}"


def test_scim_user_routes_act_in_the_cells_own_accounts() -> None:
    from server.auth import scim_routes

    assert {r.endpoint.__module__ for r in scim_routes.router.routes} == {SCIM_USERS}


def test_a_router_read_timeout_answers_503_and_writes_nothing(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_slow), **kw)
    )
    with pytest.raises(errors.BackendUnavailableError):
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert cell_pg.writes == []


def test_an_org_another_process_just_recorded_is_held(cell_pg: _PG) -> None:
    cell_pg.mode = "recording"
    cell_pg.racer = "new"
    # A platform principal never places: held only through the other process's row.
    asyncio.run(tenancy.ensure_held("new", _auth("*", Role.PLATFORM_ADMIN)))
    assert cell_pg.writes == [] and "new" in tenancy._held


def test_a_request_event_for_an_org_the_cell_does_not_hold_names_no_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from server.observability import events

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(tenancy, "_held", {"held"})

    def _event(org: str, kind: str = "request") -> events.AgentEvent:
        return events.AgentEvent(
            ts=datetime.now(UTC),
            org=org,
            namespace="ns",
            agent="a",
            user_id="u",
            session_id="s",
            operation="memory.set",
            request_id="r",
            latency_ms=3,
            success=False,
            error_code="WRONG_CELL",
            memory_key="k",
            args={"memory_key": "k"},
            kind=kind,
        )

    other = events.for_this_cell(_event("beta"))
    assert (other.org, other.namespace, other.agent, other.user_id, other.session_id) == (
        "-",
        "-",
        "-",
        "-",
        "-",
    )
    assert other.memory_key == "" and other.args == {}
    assert other.subject == "" and other.consumer == ""
    assert (other.operation, other.error_code, other.latency_ms) == ("memory.set", "WRONG_CELL", 3)
    assert events.for_this_cell(_event("held")).org == "held"
    # A worker's pass runs on the cell's own stores: its rows keep their org.
    assert events.for_this_cell(_event("beta", kind="worker")).org == "beta"
    monkeypatch.setattr(settings, "cell", "")
    assert events.for_this_cell(_event("beta")).org == "beta"


# Every op input field other than `org` that names an org, and how it is checked.
ORG_FIELDS = {
    "memory.copy_to.from_org": "the source row's scope through check_authorized (row_in_scope)",
    "memory.cross_promote.from_org": "the source row's scope through check_authorized (row_in_scope)",
    "procedure.fork.from_org": "the source row's scope through check_authorized (row_in_scope)",
}


def test_every_org_like_op_field_is_declared() -> None:
    from server.operations.registry import ALL_OPERATIONS

    found = {
        f"{op.name}.{name}"
        for op in ALL_OPERATIONS
        for name in op.input_model.model_fields
        if "org" in name and name != "org"
    }
    assert found == set(ORG_FIELDS), (
        f"declare how these org fields are checked: {found ^ set(ORG_FIELDS)}"
    )


def test_over_mcp_another_cells_org_is_a_tool_error_not_a_lost_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    async def _wrong(*_a: Any, **_k: Any) -> None:
        raise errors.WrongCellError("beta", cell="us-1")

    monkeypatch.setattr(mcp_call.gates, "authorize", _wrong)
    monkeypatch.setattr(mcp_call, "_emit_refused", lambda *_a, **_k: None)
    op = OPERATIONS_BY_NAME["memory.get"]
    obj = op.input_model.model_validate(
        {"org": "beta", "namespace": "ns", "agent": "a", "memory_key": "k"}
    )
    answer = asyncio.run(mcp_call._authorize_call(7, op, obj, {}, _auth("beta"), "req-1"))
    assert isinstance(answer, dict) and answer["id"] == 7
    assert answer["result"]["isError"] is True
    assert "WRONG_CELL" in str(answer["result"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header", [None, b"Basic Zm9vOmJhcg==", b"Bearer", b"garbage", b"Bearer x"]
)
async def test_on_a_token_route_a_bearer_that_does_not_verify_passes_to_the_route(
    monkeypatch: pytest.MonkeyPatch, header: bytes | None
) -> None:
    """The middleware checks nothing for a request it cannot attribute: no header, a
    malformed one, or a token that does not verify. The route answers it (401)."""
    from server.auth import jwt as jwt_mod
    from server.transport import tenancy_gate

    def _refuse(_token: str) -> AuthInfo:
        raise jwt_mod.TokenError("signature does not verify")

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(jwt_mod, "verify_token", _refuse)
    never = AsyncMock(side_effect=AssertionError("checked"))
    monkeypatch.setattr(tenancy, "ensure_held", never)
    path = "/v1/me/sharing-summary"
    assert tenancy_gate.acts_in_token_org(path)
    app = AsyncMock()
    headers = [] if header is None else [(b"authorization", header)]
    scope = {"type": "http", "method": "GET", "path": path, "headers": headers}
    await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), AsyncMock())
    app.assert_awaited_once()
    never.assert_not_called()


def test_a_platform_admin_never_places_even_its_own_org(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _router(monkeypatch, {"cell": "eu-1", "deployment_id": "dep-eu-1"})
    with pytest.raises(errors.WrongCellError):
        asyncio.run(tenancy.ensure_held("beta", _auth("beta", Role.PLATFORM_ADMIN)))
    assert calls == [] and cell_pg.writes == []


def test_a_winner_elsewhere_is_remembered(cell_pg: _PG, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _router(monkeypatch, {"cell": "us-1", "deployment_id": "dep-us-1"})
    for _ in range(3):
        with pytest.raises(errors.WrongCellError, match="us-1"):
            asyncio.run(tenancy.ensure_held("beta", _auth("beta")))
    assert len(calls) == 1


def test_this_cells_name_under_another_deployment_answers_503(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    _router(monkeypatch, {"cell": "eu-1", "deployment_id": "someone-else"})
    with pytest.raises(errors.BackendUnavailableError, match="another deployment id"):
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert cell_pg.writes == []


def test_a_validation_refusal_writes_no_code_trace_for_an_org_the_cell_does_not_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import common

    traced: list[str] = []
    monkeypatch.setattr(common, "schedule_code_trace", lambda **kw: traced.append(kw["org"]))
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(tenancy, "_held", {"held"})
    for org in ("beta", "held"):
        common.schedule_code_trace_validation_failure(
            "code.search", {"org": org, "namespace": "ns", "query": "q"}, [{"loc": ["x"]}]
        )
    assert traced == ["held"]


@pytest.mark.parametrize(
    "principal,held,provisions",
    [
        (("beta", Role.PLATFORM_ADMIN), set(), False),  # another org, not held here
        (("beta", Role.PLATFORM_ADMIN), {"beta"}, True),  # another org, held here
        (("acme", Role.WRITER), set(), True),  # the call's own org
    ],
)
def test_the_gate_provisions_a_personal_namespace_only_in_a_held_org(
    monkeypatch: pytest.MonkeyPatch, principal: tuple, held: set[str], provisions: bool
) -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates

    provisioned: list[str] = []

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _provision(*, org: str, **_k: Any) -> None:
        provisioned.append(org)

    async def _is_held(org: str) -> bool:
        return org in held

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(tenancy, "ensure_held", _nothing)
    monkeypatch.setattr(tenancy, "is_held", _is_held)
    monkeypatch.setattr("server.auth.authz.check_acted_for", _nothing)
    monkeypatch.setattr(
        "server.auth.personal_namespace.ensure_personal_namespace_cached", _provision
    )
    monkeypatch.setattr("server.code.app.member_writes.authorize_op", _nothing)
    monkeypatch.setattr(gates, "_admitted", _nothing)
    op = OPERATIONS_BY_NAME["memory.set"]
    target = gates.Target(org="acme", namespace="ns", named_namespace="ns", register_namespace="ns")
    asyncio.run(gates.authorize(op, _auth(*principal), target, transport="http"))
    assert provisioned == ([principal[0]] if provisions else [])


def test_is_held_reads_the_row_on_a_miss_and_places_nothing(cell_pg: _PG) -> None:
    assert asyncio.run(tenancy.is_held("held")) is True
    assert asyncio.run(tenancy.is_held("new")) is False
    cell_pg.down = True
    assert asyncio.run(tenancy.is_held("held")) is True  # remembered
    assert asyncio.run(tenancy.is_held("other")) is False  # unread: not held
    assert cell_pg.writes == []


@pytest.mark.asyncio
async def test_a_platform_admin_of_an_org_held_elsewhere_reaches_org_free_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import tenancy_gate

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(
        "server.auth.jwt.verify_token", lambda _t: _auth("ops", Role.PLATFORM_ADMIN)
    )

    async def _never(org: Any, auth: Any) -> None:
        raise errors.WrongCellError(str(org))

    monkeypatch.setattr(tenancy, "ensure_held", _never)
    headers = [(b"authorization", b"Bearer x")]
    for path in ("/v1/admin/workers.status", "/v1/admin/token.mint", "/v1/whoami"):
        app = AsyncMock()
        scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
        await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), AsyncMock())
        app.assert_awaited_once()


@pytest.mark.parametrize("mode", ["recording", "enforced"])
@pytest.mark.parametrize("org", ["new", "held", "placed-elsewhere"])
def test_a_token_of_another_org_is_refused_first_and_records_and_learns_nothing(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch, mode: str, org: str
) -> None:
    cell_pg.mode = mode
    monkeypatch.setattr(tenancy, "_elsewhere", {"placed-elsewhere": "us-1"})
    calls = _router(monkeypatch, {"cell": "eu-1", "deployment_id": "dep-eu-1"})
    with pytest.raises(PermissionError) as raised:
        asyncio.run(tenancy.ensure_held(org, _auth("acme")))
    # The same answer whether this cell holds the org, does not, or knows where it is.
    assert "us-1" not in str(raised.value) and not isinstance(raised.value, errors.WrongCellError)
    assert calls == [] and cell_pg.writes == []


@pytest.mark.asyncio
async def test_the_middleware_answers_503_while_it_cannot_judge_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import jwt
    from server.transport import tenancy_gate

    monkeypatch.setattr(settings, "cell", "eu-1")

    def _unavailable(_t: str) -> None:
        raise jwt.KeyringUnavailable("the signing keyring is not loaded yet")

    monkeypatch.setattr("server.auth.jwt.verify_token", _unavailable)
    app, send = AsyncMock(), AsyncMock()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/me/sharing-summary",
        "headers": [(b"authorization", b"Bearer x")],
    }
    await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), send)
    app.assert_not_awaited()
    start, body = send.await_args_list[0].args[0], send.await_args_list[1].args[0]
    assert start["status"] == 503 and (b"retry-after", b"5") in start["headers"]
    # The error's kind reaches the caller; its text only the log.
    assert b"KeyringUnavailable" in body["body"]
    assert b"not loaded yet" not in body["body"]


def test_an_mcp_batch_keeps_its_answers_when_one_entry_names_another_cells_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the endpoint, so the pre-dispatch namespace screen runs too."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.auth.jwt import mint_token
    from server.transport import mcp as mcp_mod

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "oauth_enabled", False)
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(cell, "gate_reason", lambda: "")

    async def _held(org: Any, auth: Any) -> None:
        if org == "beta":
            raise errors.WrongCellError("beta", cell="us-1")

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    app = FastAPI()
    mcp_mod.attach(app)
    client = TestClient(app, raise_server_exceptions=False)
    token = mint_token(
        subject="alice@beta", org="beta", namespaces=["ns"], roles=["writer"], ttl_seconds=300
    )
    batch = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "loom_memory_get",
                "arguments": {"org": "beta", "namespace": "ns", "memory_key": "k"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    r = client.post("/mcp", json=batch, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    frames = {f["id"]: f for f in r.json()}
    assert set(frames) == {1, 2}
    assert frames[1]["result"]["isError"] is True
    assert frames[1]["result"]["structuredContent"]["code"] == "WRONG_CELL"
    assert "error" not in frames[2]


def test_an_mcp_call_without_a_namespace_from_another_cells_org_answers_wrong_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's defaults live in its own org: on a cell that does not hold it, the
    call answers WRONG_CELL, not a validation failure for the missing namespace."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.auth.jwt import mint_token
    from server.transport import mcp as mcp_mod

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "oauth_enabled", False)
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(cell, "gate_reason", lambda: "")
    held: list[str] = []

    async def _held(org: Any, auth: Any) -> None:
        held.append(str(org))
        if org == "beta":
            raise errors.WrongCellError("beta", cell="us-1")

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    app = FastAPI()
    mcp_mod.attach(app)
    client = TestClient(app, raise_server_exceptions=False)
    token = mint_token(
        subject="alice@beta", org="beta", namespaces=["ns"], roles=["writer"], ttl_seconds=300
    )
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "loom_memory_set", "arguments": {"memory_key": "k", "value": "v"}},
    }
    r = client.post("/mcp", json=call, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "WRONG_CELL"
    assert held[0] == "beta"


def test_the_mcp_pre_check_uses_the_org_the_gate_would_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code.* call naming no org acts in the code plane's org: a platform admin of
    an org held elsewhere is not refused by its token's org there."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "code_org", "acme")
    checked: list[str] = []

    async def _held(org: Any, auth: Any) -> None:
        checked.append(str(org))
        raise errors.WrongCellError(str(org), cell="us-1")

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    admin = _auth("beta", Role.PLATFORM_ADMIN)
    code = OPERATIONS_BY_NAME["code.search"]
    assert asyncio.run(mcp_call._another_cells_org(1, code, {"query": "q"}, admin)) is None
    assert checked == []
    # A memory call naming no org acts in the token's own org: checked here.
    memory = OPERATIONS_BY_NAME["memory.set"]
    answer = asyncio.run(mcp_call._another_cells_org(2, memory, {"memory_key": "k"}, _auth("beta")))
    assert answer is not None and answer["result"]["isError"] is True
    assert checked == ["beta"]


def test_a_503_from_the_mcp_pre_check_is_ledgered_and_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    monkeypatch.setattr(settings, "cell", "eu-1")
    ledgered: list[str] = []

    async def _down(org: Any, auth: Any) -> None:
        raise errors.BackendUnavailableError("router down", operation="placement")

    monkeypatch.setattr(tenancy, "ensure_held", _down)
    monkeypatch.setattr(
        mcp_call,
        "emit_token_refusal",
        lambda *_a, **k: ledgered.append(f"{k['error_code']}:{k.get('server_fault')}"),
    )
    op = OPERATIONS_BY_NAME["memory.set"]
    with pytest.raises(errors.BackendUnavailableError):
        asyncio.run(mcp_call._another_cells_org(1, op, {"memory_key": "k"}, _auth("beta")))
    assert ledgered == ["BACKEND_UNAVAILABLE:True"]


@pytest.mark.asyncio
async def test_an_org_wide_token_on_the_callers_own_routes_is_checked_in_its_fallback_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import tenancy_gate

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "self_signup_org", "signups")
    monkeypatch.setattr("server.auth.jwt.verify_token", lambda _t: _auth("*"))
    checked: list[str] = []

    async def _held(org: Any, auth: Any) -> None:
        checked.append(str(org))
        raise errors.WrongCellError(str(org))

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    app, send = AsyncMock(), AsyncMock()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/me/set-default-namespace",
        "headers": [(b"authorization", b"Bearer x")],
    }
    await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), send)
    app.assert_not_awaited()
    assert checked == ["signups"]


def test_the_mcp_pre_checks_refusal_row_names_no_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import events, ledger
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(tenancy, "_held", set())
    rows: list[events.AgentEvent] = []

    class _Emitter:
        def emit(self, event: events.AgentEvent) -> None:
            rows.append(event)

    monkeypatch.setattr(ledger, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr(ledger, "_event_hold", None)

    async def _wrong(org: Any, auth: Any) -> None:
        raise errors.WrongCellError(str(org), cell="us-1")

    monkeypatch.setattr(tenancy, "ensure_held", _wrong)
    op = OPERATIONS_BY_NAME["memory.set"]
    token = _auth("beta")
    args = {"memory_key": "k", "value": "v", "agent": "a1"}
    assert asyncio.run(mcp_call._another_cells_org(1, op, args, token)) is not None
    (row,) = rows
    assert (row.org, row.namespace, row.agent, row.subject, row.args) == ("-", "-", "-", "", {})
    assert row.error_code == "WRONG_CELL"


@pytest.mark.asyncio
async def test_on_a_scim_route_the_middleware_answers_an_rfc_7644_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import tenancy_gate

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr("server.auth.jwt.verify_token", lambda _t: _auth("beta", Role.ORG_ADMIN))

    async def _wrong(org: Any, auth: Any) -> None:
        raise errors.WrongCellError(str(org), cell="us-1")

    monkeypatch.setattr(tenancy, "ensure_held", _wrong)
    app, send = AsyncMock(), AsyncMock()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/scim/v2/Groups",
        "headers": [(b"authorization", b"Bearer x")],
    }
    await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), send)
    start, body = send.await_args_list[0].args[0], send.await_args_list[1].args[0]
    assert start["status"] == 409
    assert (b"content-type", b"application/scim+json") in start["headers"]
    error = json.loads(body["body"])
    assert error["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert error["status"] == "409" and isinstance(error["detail"], str)
    assert "WRONG_CELL" in error["detail"]


def test_wrong_cell_names_the_cell_to_call_only_when_it_knows_one() -> None:
    assert "Call that cell" in errors.WrongCellError("beta", cell="us-1").message
    unplaced = errors.WrongCellError("beta").message
    assert "Call that cell" not in unplaced and "token of that org" in unplaced


@pytest.mark.asyncio
@pytest.mark.parametrize("token_org", ["", "*"])
async def test_an_org_less_or_org_wide_token_on_the_callers_routes_is_served_in_its_sign_up_org(
    monkeypatch: pytest.MonkeyPatch, token_org: str
) -> None:
    from server.transport import tenancy_gate

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "self_signup_org", "signups")
    monkeypatch.setattr("server.auth.jwt.verify_token", lambda _t: _auth(token_org))
    seen: list[tuple[str, Any]] = []

    async def _held(org: Any, auth: Any) -> None:
        seen.append((str(org), auth))

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    app = AsyncMock()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/me/sharing-summary",
        "headers": [(b"authorization", b"Bearer x")],
    }
    await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), AsyncMock())
    app.assert_awaited_once()
    assert seen == [("signups", None)]  # no principal: recorded or refused, never placed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["enforced", "recording"])
async def test_an_org_wide_tokens_sign_up_org_is_recorded_or_refused_never_placed(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """End to end through the real check: the sign-up org a route falls back to."""
    from server.transport import tenancy_gate

    cell_pg.mode = mode
    calls = _router(monkeypatch, {"cell": "eu-1", "deployment_id": "dep-eu-1"})
    monkeypatch.setattr(settings, "self_signup_org", "signups")
    monkeypatch.setattr("server.auth.jwt.verify_token", lambda _t: _auth("*"))
    app, send = AsyncMock(), AsyncMock()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/me/sharing-summary",
        "headers": [(b"authorization", b"Bearer x")],
    }
    await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), send)
    assert calls == []  # never placed
    if mode == "recording":
        app.assert_awaited_once()
        assert cell_pg.writes == ["signups"]
    else:
        app.assert_not_awaited()
        start = send.await_args_list[0].args[0]
        assert start["status"] == 409
        assert (b"link", errors.error_help_link("WRONG_CELL").encode()) in start["headers"]
        assert cell_pg.writes == []


@pytest.mark.asyncio
async def test_the_service_account_routes_act_in_the_tokens_own_org(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They do not fall back to the sign-up org: an org-wide token's `*` names none."""
    from server.transport import tenancy_gate

    cell_pg.down = True  # nothing is read for `*`
    monkeypatch.setattr(settings, "self_signup_org", "signups")
    monkeypatch.setattr("server.auth.jwt.verify_token", lambda _t: _auth("*"))
    for path in ("/v1/me/service_accounts", "/v1/me/service_accounts.create"):
        app = AsyncMock()
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"authorization", b"Bearer x")],
        }
        await tenancy_gate.TenancyMiddleware(app)(scope, AsyncMock(), AsyncMock())
        app.assert_awaited_once()
    assert cell_pg.writes == []


@pytest.mark.asyncio
async def test_the_middlewares_503_carries_the_errors_own_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import tenancy_gate

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr("server.auth.jwt.verify_token", lambda _t: _auth("acme"))

    async def _down(org: Any, auth: Any) -> None:
        raise errors.BackendUnavailableError("router down", operation="placement")

    monkeypatch.setattr(tenancy, "ensure_held", _down)
    send = AsyncMock()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/me/sharing-summary",
        "headers": [(b"authorization", b"Bearer x")],
    }
    await tenancy_gate.TenancyMiddleware(AsyncMock())(scope, AsyncMock(), send)
    detail = json.loads(send.await_args_list[1].args[0]["body"])["detail"]
    assert detail["code"] == "BACKEND_UNAVAILABLE"
    assert detail["retry_after_s"] == 1 and detail["operation"] == "placement"


def test_a_configured_org_with_no_principal_answers_503_until_attach_places_it(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JIT and self-signup act with no principal: their configured org, not held yet in
    `enforced`, is placed at attach, so they answer 503 (retry), never WRONG_CELL."""
    cell_pg.mode = "enforced"
    monkeypatch.setattr(settings, "oidc_jit_org", "acme-jit")
    calls = _router(monkeypatch, {"cell": "eu-1", "deployment_id": "dep-eu-1"})
    with pytest.raises(errors.BackendUnavailableError, match="not placed yet"):
        asyncio.run(tenancy.ensure_held("acme-jit", None))
    # An org no configuration names still answers WRONG_CELL with no principal.
    with pytest.raises(errors.WrongCellError):
        asyncio.run(tenancy.ensure_held("someone-else", None))
    assert calls == [] and cell_pg.writes == []


def test_jit_writes_no_account_in_an_org_the_cell_does_not_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import jit, self_signup

    written: list[str] = []

    async def _none(*_a: Any, **_k: Any) -> Any:
        return []

    async def _free(*_a: Any) -> tuple[Any, str]:
        return jit._Name.FREE, "alice"

    async def _provision(**kw: Any) -> None:
        written.append(kw["org"])

    monkeypatch.setattr(jit, "_accounts_naming", _none)
    monkeypatch.setattr(jit, "_free_name", _free)
    monkeypatch.setattr(self_signup, "provision_account", _provision)
    monkeypatch.setattr(
        tenancy,
        "ensure_held",
        AsyncMock(side_effect=errors.BackendUnavailableError("placed at attach")),
    )
    with pytest.raises(errors.BackendUnavailableError):
        asyncio.run(jit._provision_jit("acme-jit", "alice@acme.test", "alice"))
    assert written == []


def test_a_router_credential_of_another_form_is_refused_at_boot() -> None:
    """The directory keeps only the credential's SHA-256, so a guessable one is refused:
    it is `lcr_` and 128 lowercase hex characters, or unset."""
    from pydantic import ValidationError

    from server.settings import Settings

    for token in ("", "lcr_" + "0a" * 64):
        assert Settings(_env_file=None, cell_router_token=token).cell_router_token == token  # type: ignore[call-arg]
    for token in ("t" * 40, "lcr_" + "0" * 64, "lcr_" + "A" * 128, "lsc_" + "0" * 128):
        with pytest.raises(ValidationError, match="cell_router_token"):
            Settings(_env_file=None, cell_router_token=token)  # type: ignore[call-arg]


def test_a_router_slower_than_the_timeout_is_cut_off(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOOM_CELL_ROUTER_TIMEOUT_S cuts off a router slower than it."""
    monkeypatch.setattr(settings, "cell_router_timeout_s", 0.2)

    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"cell": "eu-1", "deployment_id": "dep-eu-1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_slow), **kw)
    )
    started = time.monotonic()
    with pytest.raises(errors.BackendUnavailableError, match="did not answer"):
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert time.monotonic() - started < 2
    assert cell_pg.writes == []


def test_requests_arriving_together_for_a_new_org_share_one_router_call(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _answer(_request: httpx.Request) -> httpx.Response:
        calls.append("place")
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"cell": "eu-1", "deployment_id": "dep-eu-1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_answer), **kw)
    )

    async def _burst() -> None:
        await asyncio.gather(*(tenancy.ensure_held("new", _auth("new")) for _ in range(5)))

    asyncio.run(_burst())
    assert calls == ["place"]
    assert "new" in cell_pg.held and tenancy._placing == {}


def test_an_org_not_held_is_read_once_per_cache_window(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    real = cell_pg.fetchval

    async def _counted(sql: str, org: str) -> Any:
        reads.append(org)
        return await real(sql, org)

    from server.core import pg

    monkeypatch.setattr(pg, "fetchval", _counted)
    assert [asyncio.run(tenancy.is_held("other")) for _ in range(3)] == [False] * 3
    assert reads == ["other"]
    tenancy._not_held["other"] -= settings.tenancy_mode_cache_s  # the window has passed
    assert asyncio.run(tenancy.is_held("other")) is False
    assert reads == ["other", "other"]


def _shared_router(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, fail: bool) -> None:
    async def _answer(_request: httpx.Request) -> httpx.Response:
        calls.append("place")
        await asyncio.sleep(0.05)
        if fail:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"cell": "eu-1", "deployment_id": "dep-eu-1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_answer), **kw)
    )


def test_a_request_cancelled_leaves_the_shared_placement_to_the_others(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _shared_router(monkeypatch, calls, fail=False)

    async def _burst() -> list[Any]:
        waiters = [asyncio.create_task(tenancy.ensure_held("new", _auth("new"))) for _ in range(3)]
        await asyncio.sleep(0.01)
        waiters[0].cancel()
        return await asyncio.gather(*waiters, return_exceptions=True)

    results = asyncio.run(_burst())
    assert isinstance(results[0], asyncio.CancelledError) and results[1:] == [None, None]
    assert calls == ["place"] and "new" in cell_pg.held


def test_a_failed_placement_every_waiter_left_is_retrieved(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _shared_router(monkeypatch, calls, fail=True)
    unhandled: list[dict] = []

    async def _all_leave() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _l, ctx: unhandled.append(ctx))
        waiters = [asyncio.create_task(tenancy.ensure_held("new", _auth("new"))) for _ in range(2)]
        await asyncio.sleep(0.01)
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        await asyncio.sleep(0.1)  # the shared call fails with no one waiting
        gc.collect()

    asyncio.run(_all_leave())
    assert calls == ["place"] and tenancy._placing == {}
    assert unhandled == [] and cell_pg.writes == []


@pytest.mark.parametrize(
    "header,wait", [("60", 60), ("7", 7), (None, 1), ("soon", 1), ("0", 1), ("3600", 60)]
)
def test_a_router_refusal_passes_its_wait_to_the_client(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch, header: str | None, wait: int
) -> None:
    """A cell past its rate is told by the router when to retry; the request's 503
    carries that wait, so the client does not retry every second."""

    _router(monkeypatch, {"error": "rate"}, 503, {"Retry-After": header} if header else None)
    with pytest.raises(errors.BackendUnavailableError) as refused:
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert refused.value.extra["retry_after_s"] == wait
    assert cell_pg.writes == []


def test_a_recording_cell_the_directory_marks_serving_records_no_new_org(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore to before attach's step 3 reads `recording` again; the directory says
    `serving`, so a new org answers 503 and nothing is written, while a held org serves."""
    cell_pg.mode = "recording"
    monkeypatch.setattr(tenancy, "directory_says_attaching", AsyncMock(return_value=False))
    with pytest.raises(errors.BackendUnavailableError, match="attaching"):
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert cell_pg.writes == [] and "new" not in cell_pg.held
    asyncio.run(tenancy.ensure_held("held", _auth("held")))


def _state_router(monkeypatch: pytest.MonkeyPatch, answer: Any, status: int = 200) -> list[str]:
    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path} {request.headers.get('authorization')}")
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(status, json=answer)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_handler), **kw)
    )
    return calls


@pytest.fixture
def state_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "cell_router_url", "https://router.test/")
    monkeypatch.setattr(settings, "cell_router_token", "lcr_" + "0" * 128)
    monkeypatch.setattr(tenancy, "_attaching", None)
    monkeypatch.setattr(tenancy, "_attaching_read", None)


@pytest.mark.parametrize(
    "answer,status,attaching",
    [
        ({"cell": "eu-1", "state": "attaching"}, 200, True),
        ({"cell": "eu-1", "state": "serving"}, 200, False),
        ({"error": "not the first cell"}, 404, False),
        ({"error": "no cell"}, 401, False),
        (["attaching"], 200, False),
        (httpx.ConnectError("down"), 200, False),
    ],
)
def test_the_state_read_is_attaching_only_on_that_answer(
    state_read: None, monkeypatch: pytest.MonkeyPatch, answer: Any, status: int, attaching: bool
) -> None:
    calls = _state_router(monkeypatch, answer, status)
    assert asyncio.run(tenancy.directory_says_attaching()) is attaching
    assert calls == [f"GET /cells/v1/cells/eu-1 Bearer lcr_{'0' * 128}"]


def test_the_state_read_is_read_once_per_window_and_shared(
    state_read: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _state_router(monkeypatch, {"cell": "eu-1", "state": "attaching"})

    async def _burst() -> list[bool]:
        return list(await asyncio.gather(*(tenancy.directory_says_attaching() for _ in range(5))))

    assert asyncio.run(_burst()) == [True] * 5
    assert asyncio.run(tenancy.directory_says_attaching()) is True
    assert len(calls) == 1
    answer, at = tenancy._attaching
    tenancy._attaching = (answer, at - settings.tenancy_mode_cache_s)  # the window has passed
    assert asyncio.run(tenancy.directory_says_attaching()) is True
    assert len(calls) == 2


def test_the_state_read_without_a_router_is_not_attaching(
    state_read: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cell_router_url", "")
    calls = _state_router(monkeypatch, {"cell": "eu-1", "state": "attaching"})
    assert asyncio.run(tenancy.directory_says_attaching()) is False and calls == []


def test_a_state_read_slower_than_the_timeout_is_not_attaching(
    state_read: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cell_router_timeout_s", 0.2)

    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"cell": "eu-1", "state": "attaching"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_slow), **kw)
    )
    started = time.monotonic()
    assert asyncio.run(tenancy.directory_says_attaching()) is False
    assert time.monotonic() - started < 2


def test_a_false_state_read_stands_for_the_window_too(
    state_read: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A router outage costs one call per window, not one per new-org request."""
    calls = _state_router(monkeypatch, httpx.ConnectError("down"))
    assert [asyncio.run(tenancy.directory_says_attaching()) for _ in range(3)] == [False] * 3
    assert len(calls) == 1


def test_a_cancelled_waiter_leaves_the_shared_state_read_to_the_others(
    state_read: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _answer(_request: httpx.Request) -> httpx.Response:
        calls.append("read")
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"cell": "eu-1", "state": "attaching"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_answer), **kw)
    )

    async def _burst() -> list[Any]:
        waiters = [asyncio.create_task(tenancy.directory_says_attaching()) for _ in range(3)]
        await asyncio.sleep(0.01)
        waiters[0].cancel()
        return await asyncio.gather(*waiters, return_exceptions=True)

    results = asyncio.run(_burst())
    assert isinstance(results[0], asyncio.CancelledError) and results[1:] == [True, True]
    assert calls == ["read"]


def test_a_recording_cell_with_no_router_names_the_settings_it_lacks(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell_pg.mode = "recording"
    monkeypatch.setattr(tenancy, "directory_says_attaching", AsyncMock(return_value=False))
    monkeypatch.setattr(settings, "cell_router_token", "")
    with pytest.raises(errors.BackendUnavailableError, match="LOOM_CELL_ROUTER_TOKEN unset"):
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert cell_pg.writes == []


def test_a_malformed_router_url_is_no_state_read_answer(
    state_read: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cell_router_url", "https://router.test:notaport")
    calls = _state_router(monkeypatch, {"cell": "eu-1", "state": "attaching"})
    assert asyncio.run(tenancy.directory_says_attaching()) is False and calls == []


def test_a_malformed_router_url_answers_a_placement_503_and_writes_nothing(
    cell_pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "cell_router_url", "https://router.test:notaport")
    calls = _router(monkeypatch, {"cell": "eu-1", "deployment_id": "dep-eu-1"})
    with pytest.raises(errors.BackendUnavailableError, match="did not answer"):
        asyncio.run(tenancy.ensure_held("new", _auth("new")))
    assert calls == [] and cell_pg.writes == []
