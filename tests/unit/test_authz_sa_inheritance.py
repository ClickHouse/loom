"""authz: a service-account (sa:) subject inherits its creator's namespace grant.

resolve_my_namespaces (the search fan-out set) inherits an sa: token's creator
grants, and so must the authz GATE (_lookup_grant / check_authorized), or
loom_search resolves a granted namespace into its set and then 403s at the
gate. This pins the gate-layer inheritance. Pure unit: a fake CH routes the
service_accounts (creator) + namespace_grants (role) lookups; no live CH.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import authz
from server.operations.base import Role


class _FakeCH:
    def __init__(
        self,
        *,
        creator: str | None,
        grant_holder: str | None,
        role: str = "admin",
        inherits: bool = True,
    ) -> None:
        self._creator = creator
        self._inherits = inherits
        self._grant_holder = grant_holder
        self._role = role

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if (
            "service_accounts" in sql and "namespace_grants" not in sql
        ):  # not the grant read's dead-token set
            if not self._creator:
                return None
            return {"created_by": self._creator, "inherit_creator_grants": self._inherits}
        if "namespace_grants" in sql:
            return (
                {"role": self._role}
                if self._grant_holder in [params.get("sub"), *params.get("subs", [])]
                else None
            )  # The alias pair rides `subs`
        return None


def _patch(monkeypatch: pytest.MonkeyPatch, ch: _FakeCH) -> None:
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)


def test_sa_subject_inherits_creator_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    # sa: token has no grant of its own; creator 'alice' is granted on the ns.
    _patch(monkeypatch, _FakeCH(creator="alice", grant_holder="alice", role="admin"))
    role = asyncio.run(authz._lookup_grant("dev", "qa-team", "sa:claude-desktop-x"))
    assert role == Role.ADMIN  # inherited from the creator


def test_sa_subject_no_creator_grant_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Creator exists but has no grant on this ns → no inheritance.
    _patch(monkeypatch, _FakeCH(creator="alice", grant_holder="bob"))
    assert asyncio.run(authz._lookup_grant("dev", "qa-team", "sa:claude-desktop-x")) is None


def test_non_sa_subject_does_not_consult_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    # A normal subject with no direct grant gets None — no service_accounts hop.
    _patch(monkeypatch, _FakeCH(creator="alice", grant_holder="alice"))
    assert asyncio.run(authz._lookup_grant("dev", "qa-team", "bob")) is None


def test_sa_subject_with_own_grant_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct grant on the sa: subject itself wins without a creator lookup.
    _patch(monkeypatch, _FakeCH(creator=None, grant_holder="sa:claude-desktop-x"))
    assert asyncio.run(authz._lookup_grant("dev", "qa-team", "sa:claude-desktop-x")) == Role.ADMIN


def test_sa_subject_that_does_not_inherit_gets_no_creator_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An agent-signup token (inherit_creator_grants=false) reads nothing through
    # its creator, as the namespace listings already decide.
    _patch(monkeypatch, _FakeCH(creator="alice", grant_holder="alice", inherits=False))
    assert asyncio.run(authz._lookup_grant("dev", "qa-team", "sa:agent-box-1")) is None
