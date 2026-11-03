"""grants.share_agent / unshare_agent: an agent lends only its own namespace,
only to agents of its own trust, and nobody else calls them."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from server.auth import agents, authz, machines
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core import grant_cache
from server.core.errors import (
    AuthInsufficientError,
    GrantWouldDowngradeError,
    PreconditionFailedError,
    ValidationError,
)
from server.identity.api.models_grants import (
    GrantsCreateOutput,
    GrantsRevokeOutput,
    GrantsShareAgentInput,
    GrantsUnshareAgentInput,
)
from server.identity.app import agent_shares
from server.identity.domain.grants import GrantState
from server.operations.base import Role

_NOW = datetime(2026, 10, 20, 12, 0, 0)


@pytest.fixture
def world(monkeypatch) -> dict[str, Any]:
    """The grantee's grant ranks, the active and the restricted namespaces,
    the trust's shared roles, the row an unshare finds and the row a share
    would write over."""
    state: dict[str, Any] = {
        "ranks": {},
        "inactive": set(),
        "restricted": set(),
        "trust_roles": [],
        "row": _row("sa:fleet:a"),
        "existing": None,
    }

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks=dict(state["ranks"]))

    async def active_namespaces(org: str, names: list[str]) -> set[str]:
        return {ns for ns in names if ns not in state["inactive"]}

    async def get(name: str) -> Any:
        return {"name": name, "org": "acme", "ns_roles": state["trust_roles"]}

    async def may_be_restricted(org: str, namespace: str) -> bool:
        return namespace in state["restricted"]

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(machines, "active_namespaces", active_namespaces)
    monkeypatch.setattr(machines, "get", get)
    monkeypatch.setattr(authz, "may_be_restricted", may_be_restricted)
    return state


def _row(granted_by: str, role: str = "viewer") -> GrantState:
    return GrantState(
        role=role, granted_by=granted_by, status="active", pending_role=None, pending_since=None
    )


@pytest.fixture
def calls(monkeypatch, world) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []

    async def create(input: Any, may_replace: Any) -> GrantsCreateOutput:
        if world["existing"] is not None:
            may_replace(world["existing"])  # as create_grant checks it, under the claim
        seen.append(("create", input))
        return GrantsCreateOutput(
            org=input.org,
            namespace=input.namespace,
            subject=input.subject,
            role=input.role,
            granted_at=_NOW,
        )

    async def revoke(input: Any, may_take: Any) -> GrantsRevokeOutput:
        may_take(world["row"])  # as revoke_grant checks it, before and under the claim
        seen.append(("revoke", input))
        return GrantsRevokeOutput(revoked=True, revoked_at=_NOW)

    monkeypatch.setattr(agent_shares, "create_grant", create)
    monkeypatch.setattr(agent_shares, "revoke_grant", revoke)
    yield seen
    set_current_auth(None)


def _as(subject: str, org: str = "acme") -> None:
    set_current_auth(
        AuthInfo(
            subject=subject,
            org=org,
            namespaces=("fleet-a",),
            roles=(),
            is_service_account=True,
            ns_roles=(("fleet-a", "writer"),),
        )
    )


def _share(**over: Any) -> Any:
    body = {"org": "acme", "namespace": "fleet-a", "grantee": "b"} | over
    return asyncio.run(agent_shares.handle_grants_share_agent(GrantsShareAgentInput(**body)))


def test_an_agent_lends_its_namespace_to_an_agent_of_its_trust(calls) -> None:
    _as("sa:fleet:a")
    out = _share()
    [(op, grant)] = calls
    assert op == "create" and grant.subject == "sa:fleet:b" and grant.role == "viewer"
    assert (grant.namespace, grant.replace) == ("fleet-a", False)  # an admin's grant is kept
    assert out.subject == "sa:fleet:b" and out.role == "viewer" and out.granted_at == _NOW
    assert _share(role="writer").role == "writer"


def _unshare() -> Any:
    body = GrantsUnshareAgentInput(org="acme", namespace="fleet-a", grantee="b")
    return asyncio.run(agent_shares.handle_grants_unshare_agent(body))


def test_an_agent_takes_its_share_back(calls) -> None:
    _as("sa:fleet:a")
    out = _unshare()
    [(op, revoke)] = calls
    assert op == "revoke" and revoke.subject == "sa:fleet:b" and revoke.namespace == "fleet-a"
    assert out.revoked and out.revoked_at == _NOW


@pytest.mark.parametrize(
    "row",
    [_row("alice@acme.com"), _row("sa:fleet:a", role="admin"), _row("sa:fleet:c")],
    ids=["an-admins-grant", "an-admin-role", "another-agents"],
)
def test_an_agent_takes_back_only_what_it_lent(calls, world, row: GrantState) -> None:
    _as("sa:fleet:a")
    world["row"] = row
    with pytest.raises(PreconditionFailedError):
        _unshare()
    assert calls == []


def test_a_restricted_namespace_is_shared_only_by_its_admin(calls, world) -> None:
    _as("sa:fleet:a")
    world["restricted"].add("fleet-a")
    with pytest.raises(AuthInsufficientError):
        _share()
    assert calls == []


@pytest.mark.parametrize(
    "row",
    [_row("alice@acme.com"), _row("alice@acme.com", role="writer"), _row("sa:fleet:a", "admin")],
    ids=["an-admins-viewer", "an-admins-writer", "an-admin-role"],
)
def test_a_share_never_writes_over_a_grant_someone_else_made(calls, world, row) -> None:
    # Else the lender would own the row, and could take an admin's grant back.
    _as("sa:fleet:a")
    world["existing"] = row
    with pytest.raises(PreconditionFailedError):
        _share(role="writer")
    assert calls == []


def test_an_agent_raises_its_own_share(calls, world) -> None:
    _as("sa:fleet:a")
    world["existing"] = _row("sa:fleet:a")
    assert _share(role="writer").role == "writer"


def test_the_limit_counts_what_the_grantees_token_would_name(calls, world) -> None:
    _as("sa:fleet:a")
    released = {f"gone-{i:03}" for i in range(agents.TOKEN_NAMESPACE_BYTES // 31)}  # 31 bytes each
    world["ranks"] = {ns: 1 for ns in released} | {"docs": 1}
    world["inactive"] = released
    world["trust_roles"] = [("docs", "viewer")]  # its trust names it already
    assert _share().role == "viewer"


def _filling(own: list[str]) -> dict[str, int]:
    """Lent namespaces that leave the token less room than fleet-a's 29 bytes."""
    full: dict[str, int] = {}
    used = agents.names_bytes(own)
    i = 0
    while used + agents.token_bytes("fleet-a") <= agents.TOKEN_NAMESPACE_BYTES:
        ns = f"ns-{i:03}"
        full[ns] = 1
        used += agents.token_bytes(ns)
        i += 1
    return full


def test_a_share_that_raises_a_role_the_trust_gives_costs_nothing(calls, world) -> None:
    _as("sa:fleet:a")
    world["trust_roles"] = [("fleet-a", "viewer")]  # the trust lends a's namespace to every agent
    world["ranks"] = _filling(own=["fleet-b", "fleet-a"])
    assert _share(role="writer").role == "writer"


def test_a_trust_namespace_no_longer_active_is_not_counted(calls, world) -> None:
    # The token names only the trust's live namespaces; a deactivated one must
    # not take room a share would fit in.
    _as("sa:fleet:a")
    gone = "g" + "x" * 127
    world["trust_roles"] = [(gone, "viewer")]
    world["inactive"] = {gone}
    world["ranks"] = _filling(own=["fleet-b"])
    for ns in list(world["ranks"])[-10:]:  # make room for fleet-a, but not for gone too
        del world["ranks"][ns]
    assert agents.names_bytes(["fleet-b", *world["ranks"], gone, "fleet-a"]) > (
        agents.TOKEN_NAMESPACE_BYTES
    )
    assert _share().role == "viewer"


def test_an_agent_whose_lent_names_fill_its_token_takes_no_new_one(calls, world) -> None:
    _as("sa:fleet:a")
    full = _filling(own=["fleet-b"])
    assert agents.names_bytes(["fleet-b", *full, "fleet-a"]) > agents.TOKEN_NAMESPACE_BYTES
    world["ranks"] = full | {"fleet-b": 3}
    with pytest.raises(PreconditionFailedError):
        _share()
    world["ranks"]["fleet-a"] = 1  # one it holds already may be raised
    assert _share(role="writer").role == "writer"
    del world["ranks"][next(iter(full))]
    del world["ranks"]["fleet-a"]
    assert _share().role == "viewer"  # fleet-a fits; b's own namespace is charged once, not again


@pytest.mark.parametrize(
    "subject, org, namespace",
    [
        ("sa:fleet:a", "acme", "fleet-b"),  # another agent's namespace
        ("sa:fleet:a", "acme", "docs"),  # a namespace the trust shares
        ("sa:other:a", "acme", "fleet-a"),  # an agent of another trust
        ("sa:fleet:a", "beta", "fleet-a"),  # its namespace in another org
        ("sa:fleet", "acme", "fleet-a"),  # the trust itself
        ("alice@acme.com", "acme", "fleet-a"),  # a person, even an admin there
    ],
)
def test_nobody_else_shares_and_no_other_namespace_is_shared(
    calls, subject: str, org: str, namespace: str
) -> None:
    _as(subject, org)
    with pytest.raises(AuthInsufficientError):
        _share(namespace=namespace)
    assert calls == []


def test_with_auth_off_there_is_no_agent_to_share(calls) -> None:
    set_current_auth(None)
    with pytest.raises(AuthInsufficientError):
        _share()
    assert calls == []


@pytest.mark.parametrize("grantee", ["a", "B", "b:c", "-b", "b" * 64, ""])
def test_the_grantee_is_another_agent_named_as_loom_takes_it(calls, grantee: str) -> None:
    _as("sa:fleet:a")
    with pytest.raises(ValidationError):
        _share(grantee=grantee)
    assert calls == []


def test_a_share_never_grants_admin() -> None:
    with pytest.raises(ValueError):
        GrantsShareAgentInput(org="acme", namespace="fleet-a", grantee="b", role="admin")


def test_the_ops_need_only_the_agents_writer_role_on_its_namespace() -> None:
    from server.identity.api import ops_grants

    for op in (ops_grants.grants_share_agent, ops_grants.grants_unshare_agent):
        assert op.required_role is Role.WRITER and op.audit


def test_an_unshare_rechecks_the_row_under_the_grants_claim(monkeypatch) -> None:
    # The grant read first is the agent's own; by the time the claim is held
    # an admin has written over it, and nothing is revoked.
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import grants as grants_app

    reads = iter([_row("sa:fleet:a"), _row("alice@acme.com")])
    written: list[Any] = []

    async def find_any_grant(org: str, namespace: str, subject: str, **kw: Any) -> GrantState:
        return next(reads)

    async def stored(org: str, namespace: str, spellings: list[str], **kw: Any) -> list[str]:
        return spellings

    class _Claim:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def insert_row(row: Any, **kw: Any) -> None:
        written.append(row)

    monkeypatch.setattr(grants_app.grants_repo, "find_any_grant", find_any_grant)
    monkeypatch.setattr(grants_app.grants_repo, "stored_spellings", stored)
    monkeypatch.setattr(grants_app.grants_repo, "insert_row", insert_row)
    monkeypatch.setattr(grants_app, "grant_change", lambda *a: _Claim())
    check = agent_shares._made_by("sa:fleet:a", "sa:fleet:b", "fleet-a")
    body = GrantsRevokeInput(org="acme", namespace="fleet-a", subject="sa:fleet:b")
    with pytest.raises(PreconditionFailedError):
        asyncio.run(grants_app.revoke_grant(body, check))
    assert written == []


@pytest.mark.parametrize(
    "made_by, role, error, refusal",
    [
        # As the admin's, not as a downgrade the agent could undo.
        ("alice@acme.com", "viewer", PreconditionFailedError, "made by alice@acme.com"),
        ("alice@acme.com", "writer", PreconditionFailedError, "made by alice@acme.com"),
        # The agent's own: it unshares, then shares again.
        ("sa:fleet:a", "viewer", GrantWouldDowngradeError, "would lower"),
    ],
)
def test_a_share_refuses_the_row_read_under_the_grants_claim(
    monkeypatch, made_by: str, role: str, error: type[Exception], refusal: str
) -> None:
    # The row read under the claim is a writer grant: the share writes nothing.
    from server.auth import aliases
    from server.identity.api.models_grants import GrantsCreateInput
    from server.identity.app import grants as grants_app

    written: list[Any] = []

    async def find_active_grant(org: str, namespace: str, subject: str, **kw: Any) -> GrantState:
        return _row(made_by, role="writer")

    async def canonical(org: str, subject: str) -> str:
        return subject

    async def no_ceiling(*a: Any) -> None:
        return None

    class _Claim:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def insert_row(row: Any, **kw: Any) -> None:
        written.append(row)

    monkeypatch.setattr(grants_app.grants_repo, "find_active_grant", find_active_grant)
    monkeypatch.setattr(grants_app.grants_repo, "insert_row", insert_row)
    monkeypatch.setattr(grants_app, "grant_change", lambda *a: _Claim())
    monkeypatch.setattr(grants_app, "_enforce_code_ceiling", no_ceiling)
    monkeypatch.setattr(aliases, "canonical_subject", canonical)
    check = agent_shares._made_by("sa:fleet:a", "sa:fleet:b", "fleet-a")
    body = GrantsCreateInput(org="acme", namespace="fleet-a", subject="sa:fleet:b", role=role)
    with pytest.raises(error, match=refusal):
        asyncio.run(grants_app.create_grant(body, check))
    assert written == []


def test_a_share_that_would_lower_a_grant_says_how_an_agent_lowers_it(monkeypatch, world) -> None:
    async def lowering(input: Any, may_replace: Any) -> Any:
        raise GrantWouldDowngradeError("", current_role="writer", requested_role=input.role)

    monkeypatch.setattr(agent_shares, "create_grant", lowering)
    _as("sa:fleet:a")
    try:
        with pytest.raises(GrantWouldDowngradeError, match="grants.unshare_agent") as e:
            _share()
        assert e.value.extra == {"current_role": "writer", "requested_role": "viewer"}
    finally:
        set_current_auth(None)


@pytest.mark.parametrize("op", ["grants.share_agent", "grants.unshare_agent"])
def test_a_share_naming_no_grantee_fails_validation_over_http(monkeypatch, op: str) -> None:
    # Clients fill `agent` from the caller; the grantee is its own field, so a
    # call that leaves it out is refused, never a grant to the caller's agent.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.settings import settings
    from server.transport.http import build_router

    monkeypatch.setattr(settings, "identity_provider", "single-user")
    app = FastAPI()
    app.include_router(build_router())
    body = {"org": "acme", "namespace": "fleet-a", "agent": "a"}
    r = TestClient(app).post(f"/v1/{op}", json=body)
    assert r.status_code == 422 and "grantee" in r.text


@pytest.mark.parametrize("op", ["grants.share_agent", "grants.unshare_agent"])
def test_a_share_naming_no_grantee_fails_validation_over_mcp(monkeypatch, op: str) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.transport.mcp as mcp
    from server.settings import settings

    monkeypatch.setattr(settings, "identity_provider", "single-user")
    app = FastAPI()
    mcp.attach(app)
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "loom_op",
            # `agent` as a client fills it from the caller, and no grantee.
            "arguments": {
                "op": op,
                "arguments": {"org": "acme", "namespace": "fleet-a", "agent": "a"},
            },
        },
    }
    r = TestClient(app).post("/mcp", json=call)
    result = r.json()["result"]
    assert result["isError"] and "grantee" in json.dumps(result)


def test_the_grantee_is_never_an_identity_field_clients_fill() -> None:
    from server.transport._common import IDENTITY_FIELDS
    from server.transport.arg_check import IDENTITY_KEYS

    assert "grantee" not in IDENTITY_FIELDS and "grantee" not in IDENTITY_KEYS
    with pytest.raises(ValueError, match="grantee"):
        GrantsShareAgentInput.model_validate({"org": "acme", "namespace": "fleet-a", "agent": "a"})
