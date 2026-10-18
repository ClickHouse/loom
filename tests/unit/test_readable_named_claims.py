"""The namespaces the MCP transport tells a token to name are exactly the
named claims the gate lets it read: `readable_named_claims` and
`authz.check_authorized(VIEWER, ns)` agree for every kind of token."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import authz
from server.auth.context import readable_named_claims
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "o"
RESTRICTED = frozenset({"secret"})

# (subject, namespace) -> live grant rank; 0 is a grant row no longer live.
GRANTS = {
    ("alice", "team"): 2,
    ("alice", "secret"): 1,
    ("alice", "left"): 0,
    ("alice", "gone"): 0,
    ("sa:cli", "team"): 0,
    ("sa:inherits", "team"): 0,
    ("sa:inherits", "solo"): 0,
}


@pytest.fixture(autouse=True)
def _grants(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.delegation as delegation
    import server.auth.service_accounts as service_accounts
    import server.core.grant_cache as grant_cache
    import server.identity.app.namespace_classification as nc

    async def _read(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={ns: r for (s, ns), r in GRANTS.items() if s == subject})

    async def _grant_role_for(org: str, namespace: str, subject: str) -> Role | None:
        rank = GRANTS.get((subject, namespace), 0)
        return {1: Role.VIEWER, 2: Role.WRITER, 3: Role.ADMIN}.get(rank)

    async def _inheriting_creator(org: str, subject: str, **_: Any) -> str | None:
        return "alice" if subject == "sa:inherits" else None

    async def _active(org: str, creator: str) -> bool:
        return True

    async def _restricted_set(org: str) -> frozenset[str]:
        return RESTRICTED

    monkeypatch.setattr(grant_cache, "_read", _read)
    monkeypatch.setattr(authz, "grant_role_for", _grant_role_for)
    monkeypatch.setattr(service_accounts, "inheriting_creator", _inheriting_creator)
    monkeypatch.setattr(delegation, "creator_account_active", _active)
    monkeypatch.setattr(nc, "restricted_set", _restricted_set)


def _token(subject: str, namespaces: tuple[str, ...], **kw: Any) -> AuthInfo:
    base: dict[str, Any] = {"org": ORG, "roles": (Role.WRITER,), "is_service_account": False}
    return AuthInfo(subject=subject, namespaces=namespaces, **{**base, **kw})


TOKENS = {
    "flat human": _token("alice", ("team", "home", "gone")),
    "scoped human": _token(
        "alice", ("team", "gone"), ns_roles=(("team", "viewer"), ("gone", "writer"))
    ),
    "delegated, own grant revoked, creator holds it": _token(
        "sa:cli", ("team",), creator="alice", is_service_account=True
    ),
    "delegated, creator lost it": _token(
        "sa:cli", ("left", "team"), creator="alice", is_service_account=True
    ),
    "inheriting service account, own claim capped": _token(
        "sa:inherits", ("team", "solo"), is_service_account=True
    ),
    "restricted claims": _token("alice", ("secret", "team")),
    "restricted claim without a grant": _token("bob", ("secret", "home")),
    "delegated restricted claim": _token(
        "sa:cli", ("secret", "team"), creator="alice", is_service_account=True
    ),
    "token of another org": _token("alice", ("team", "home"), org="other"),
    "org-wide token": _token("alice", ("secret", "team"), org="*"),
}


async def _gate_reads(token: AuthInfo, namespace: str) -> bool:
    try:
        await authz.check_authorized(token, Role.VIEWER, ORG, namespace)
    except PermissionError:
        return False
    return True


@pytest.mark.parametrize("kind", list(TOKENS))
def test_the_named_claims_are_the_ones_the_gate_lets_the_token_read(kind: str) -> None:
    token = TOKENS[kind]

    async def _both() -> tuple[list[str], list[str]]:
        admitted = [ns for ns in token.namespaces if await _gate_reads(token, ns)]
        return await readable_named_claims(token, ORG), admitted

    readable, admitted = asyncio.run(_both())
    assert readable == admitted


def test_the_cases_cover_both_outcomes() -> None:
    # Guard against a fixture that admits (or refuses) everything.
    async def _outcomes() -> set[bool]:
        return {await _gate_reads(t, ns) for t in TOKENS.values() for ns in t.namespaces}

    assert asyncio.run(_outcomes()) == {True, False}


def test_no_token_names_nothing() -> None:
    assert asyncio.run(readable_named_claims(None, ORG)) == []
