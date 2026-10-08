"""resolve_my_namespaces: service-account tokens inherit their creator's grants.

An sa:<client>-<hash> subject has no grants of its own; it must union the
created_by user's live namespace_grants (default-on, opt-out via
inherit_creator_grants=false). Pure unit: a fake CH routes the three queries
(own grants / sa lookup / creator grants) by SQL + subject; no live CH.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth.context import resolve_my_namespaces, set_current_auth
from server.auth.jwt import AuthInfo


class _FakeCH:
    def __init__(self, *, sa_row: dict[str, Any] | None, creator_grants: list[str]) -> None:
        self._sa_row = sa_row
        self._creator_grants = creator_grants

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        subs = [params["sub"]] if params.get("sub") else list(params.get("subs") or [])
        if "service_accounts" in sql:
            return [self._sa_row] if self._sa_row else []
        if "namespace_grants" in sql:
            # Grants belong only to creator 'alice'; every other subject (the
            # sa: token itself, or an unrelated user) has none of their own.
            if "alice" in subs:
                return [{"namespace": ns} for ns in self._creator_grants]
            return []
        return []


def _run(
    org: str,
    *,
    sa_row: dict[str, Any] | None,
    creator_grants: list[str],
    subject: str,
    claim: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    monkeypatch.setattr(
        "server.core.ch.get_client", lambda: _FakeCH(sa_row=sa_row, creator_grants=creator_grants)
    )
    # resolve_my_namespaces only reads subject + namespaces; roles can be empty.
    set_current_auth(
        AuthInfo(
            subject=subject, org=org, namespaces=tuple(claim), roles=(), is_service_account=True
        )
    )
    try:
        return asyncio.run(resolve_my_namespaces(org))
    finally:
        set_current_auth(None)


def test_sa_token_inherits_creator_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        "dev",
        sa_row={"created_by": "alice", "inherit_creator_grants": True},
        creator_grants=["qa-team", "shared-x"],
        subject="sa:claude-desktop-abc",
        claim=["home"],  # the sa token's own claim
        monkeypatch=monkeypatch,
    )
    # Own claim + inherited creator grants, deduped + sorted.
    assert out == ["home", "qa-team", "shared-x"]


def test_sa_token_opts_out_of_inheritance(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        "dev",
        sa_row={"created_by": "alice", "inherit_creator_grants": False},
        creator_grants=["qa-team"],
        subject="sa:claude-desktop-abc",
        claim=["home"],
        monkeypatch=monkeypatch,
    )
    assert out == ["home"]  # no inheritance


def test_non_sa_subject_does_not_trigger_inheritance(monkeypatch: pytest.MonkeyPatch) -> None:
    # A normal user subject must not hit the sa lookup / creator-grant path.
    out = _run(
        "dev",
        sa_row={"created_by": "alice", "inherit_creator_grants": True},
        creator_grants=["qa-team"],
        subject="bob",  # a normal user, not an sa: subject, not the creator
        claim=["home"],
        monkeypatch=monkeypatch,
    )
    assert out == ["home"]  # no sa lookup, no own grants → claim only


def test_a_token_of_another_org_resolves_no_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JWT claim and the subject's grants are the token's own org's: asked
    for another org, a same-named subject's grants there and the claim's names
    are not this caller's."""
    got = _run(
        "acme",
        sa_row=None,
        creator_grants=["acme-private"],
        subject="alice",
        claim=["team"],
        monkeypatch=monkeypatch,
    )
    assert got == ["acme-private", "team"]
    monkeypatch.setattr(
        "server.core.ch.get_client", lambda: _FakeCH(sa_row=None, creator_grants=["acme-private"])
    )
    set_current_auth(
        AuthInfo(
            subject="alice", org="other", namespaces=("team",), roles=(), is_service_account=False
        )
    )
    try:
        assert asyncio.run(resolve_my_namespaces("acme")) == []
    finally:
        set_current_auth(None)
