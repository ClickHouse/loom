"""A person's membership list folds their spellings: a grant written under
the email the directory ties to the login handle (an operator's, an
invitation's, a sync's — before the person ever signed in) is listed when
they arrive under the handle, as the read gate already counts it; a
namespace granted under both spellings is one row, the highest role wins."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.identity.app.namespace as ns_mod
import server.identity.app.namespace_membership as membership
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "nerve-dev"
HANDLE = "u-jdoe-1f2e3d4c"  # the JIT self-signup row's owner_email
EMAIL = "jdoe@example.com"  # its contact_email: what grants name


def _grant(
    ns: str, subject: str, role: str = "viewer", *, day: int = 18, is_new: int = 0
) -> dict[str, Any]:
    return {
        "namespace": ns,
        "role": role,
        "status": "active",
        "pending_role": None,
        "pending_since": None,
        "joined_at": datetime(2026, 9, day),
        "visibility": "private",
        "kind": "memory",
        "description": "",
        "is_new": is_new,
        "_subject": subject,
    }


class _FakeCH:
    def __init__(self, grants: list[dict[str, Any]]):
        self.grants, self.subs = grants, []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        subs = list((params or {}).get("subs") or [])
        self.subs.append(subs)
        rows = [dict(g) for g in self.grants if g["_subject"] in set(subs)]
        return sorted(rows, key=lambda r: r["joined_at"], reverse=True)

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        return None  # not a service account: nothing inherited


@pytest.fixture
def person(monkeypatch: pytest.MonkeyPatch) -> AuthInfo:
    import server.auth.aliases as aliases
    import server.auth.context as ctx

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        assert org == ORG
        return [subject, EMAIL] if subject == HANDLE else [subject]

    auth = AuthInfo(
        subject=HANDLE,
        org=ORG,
        namespaces=(HANDLE,),
        roles=(Role.ADMIN,),
        is_service_account=False,
    )
    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(ns_mod, "resolve_subject", lambda s: s or HANDLE)
    monkeypatch.setattr(ctx, "current_auth", lambda: auth)
    return auth


async def _list_mine(monkeypatch: pytest.MonkeyPatch, fake: _FakeCH) -> dict[str, Any]:
    from server.identity.api.models import NamespaceListMineInput

    monkeypatch.setattr(membership, "get_client", lambda: fake)
    out = await membership.handle_namespace_list_mine(
        NamespaceListMineInput(org=ORG, subject=HANDLE)
    )
    return {r.namespace: r for r in out.rows}


@pytest.mark.asyncio
async def test_grants_under_the_email_are_listed_for_the_login_handle(
    person: AuthInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeCH(
        [
            _grant(HANDLE, HANDLE, "admin"),  # the personal namespace, under the handle
            _grant("nerve-tenant-agent-1", EMAIL, "writer", is_new=1),  # a sync's grant
            _grant("t-tenant-shared", EMAIL, "viewer"),
            _grant("someone-elses", "zed@example.com", "admin"),
        ]
    )
    rows = await _list_mine(monkeypatch, fake)
    assert set(rows) == {HANDLE, "nerve-tenant-agent-1", "t-tenant-shared"}
    assert rows["nerve-tenant-agent-1"].role == "writer"
    assert rows["nerve-tenant-agent-1"].is_new is True
    assert rows["t-tenant-shared"].role == "viewer"
    assert not any(r.inherited for r in rows.values())
    # the membership read asked for both spellings at once
    assert fake.subs[0] == [HANDLE, EMAIL]


@pytest.mark.asyncio
async def test_a_namespace_granted_under_both_spellings_is_one_row(
    person: AuthInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeCH(
        [
            _grant(HANDLE, HANDLE, "admin"),
            _grant("team", HANDLE, "viewer", day=20, is_new=0),  # newer, seen
            _grant("team", EMAIL, "admin", day=10, is_new=1),  # older, higher role
        ]
    )
    rows = await _list_mine(monkeypatch, fake)
    assert set(rows) == {HANDLE, "team"} and len(rows) == 2
    assert rows["team"].role == "admin"  # the highest role wins
    assert rows["team"].is_new is False  # seen under one spelling = seen
    assert rows["team"].joined_at == datetime(2026, 9, 20)  # the newest grant stands


@pytest.mark.asyncio
async def test_a_subject_with_no_aliases_lists_its_own_rows_only(
    person: AuthInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.auth.context as ctx

    other = "u-solo-1a2b3c4d"
    auth = AuthInfo(
        subject=other, org=ORG, namespaces=(other,), roles=(Role.ADMIN,), is_service_account=False
    )
    monkeypatch.setattr(ns_mod, "resolve_subject", lambda s: other)
    monkeypatch.setattr(ctx, "current_auth", lambda: auth)
    fake = _FakeCH([_grant(other, other, "admin"), _grant("nerve-tenant-agent-1", EMAIL, "writer")])
    rows = await _list_mine(monkeypatch, fake)
    assert set(rows) == {other}
    assert fake.subs[0] == [other]
