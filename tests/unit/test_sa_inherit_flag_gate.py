"""A service account's `inherit_creator_grants` decides the gate and the
listings alike.

`resolve_my_namespaces` (the fan-out set and every org-wide listing) and
`check_authorized` (the gate) must agree on whether a service account reads
through its creator's grants: an agent-signup token
(`inherit_creator_grants=False`) that the listing leaves out of a namespace
is refused there too, and a setup-flow token (`True`) is admitted by both.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth.authz import check_authorized
from server.auth.context import resolve_my_namespaces, set_current_auth
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG, TEAM, SA, CREATOR = "o", "team", "sa:agent-box-1", "alice"

# The listing asks the gate, which reads the grant maps from the fake below.
pytestmark = pytest.mark.claim_cap


class _Directory:
    async def is_restricted(self, org: str, namespace: str) -> bool:
        return False

    async def restricted_set(self, org: str) -> frozenset[str]:
        return frozenset()

    def config_classification(self, org: str, namespace: str) -> str:
        return "standard"

    def code_entry(self, org: str, namespace: str) -> None:
        return None


class _CH:
    """service_accounts: one row for SA; namespace_grants: CREATOR holds a
    viewer grant on TEAM."""

    def __init__(self, inherit: bool) -> None:
        self.sa_row = {"created_by": CREATOR, "inherit_creator_grants": inherit}

    def _grants(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        subs = params.get("subs") or [params.get("sub")]
        return [{"namespace": TEAM, "role": "viewer"}] if CREATOR in subs else []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = params or {}
        if (
            "service_accounts" in sql and "namespace_grants" not in sql
        ):  # not the grant read's dead-token set
            return [self.sa_row] if params.get("sub") == SA else []
        if "groupArray((namespace, rank))" in sql:  # the grant map (`grant_cache`)
            grants = [(TEAM, 1)] if params.get("sub") == CREATOR else []
            return [{"grants": grants, "spellings": [params.get("sub")]}]
        if "namespace_grants" in sql:
            if params.get("ns") not in (None, TEAM):
                return []
            return self._grants(params)
        return []


def _sa() -> AuthInfo:
    return AuthInfo(
        subject=SA,
        org=ORG,
        namespaces=("agent-box",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    import server.auth.aliases as aliases
    import server.auth.directory as directory_mod

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject]

    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(directory_mod, "_directory", _Directory())

    def _wire(inherit: bool) -> None:
        ch = _CH(inherit)
        monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    yield _wire
    set_current_auth(None)


def _listed(auth: AuthInfo) -> bool:
    set_current_auth(auth)
    return TEAM in asyncio.run(resolve_my_namespaces(ORG))


def _admitted(auth: AuthInfo) -> bool:
    try:
        asyncio.run(check_authorized(auth, Role.VIEWER, ORG, TEAM))
    except PermissionError:
        return False
    return True


def test_an_inheriting_service_account_is_listed_and_admitted(wire) -> None:
    wire(True)
    assert _listed(_sa()) and _admitted(_sa())


def test_a_non_inheriting_service_account_is_not_listed(wire) -> None:
    wire(False)
    assert not _listed(_sa())


def test_a_non_inheriting_service_account_is_refused_by_the_gate(wire) -> None:
    wire(False)
    assert not _admitted(_sa())
