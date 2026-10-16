"""`claim_release` retires a claim: grants revoked, the credential cleared, the row kept (and the name reserved) with every other column carried forward.

Only an org/platform admin of the namespace's org may release it; a
restricted namespace needs the caller's own direct admin grant and is
otherwise answered like a missing name; the directory row keeps every column
but the claim. Hermetic: ClickHouse and the classification are faked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import revocation
from server.auth.jwt import mint_token
from server.identity.app import claim_release as cr
from server.identity.app import namespace_classification as nc
from server.settings import settings

_ROW: dict[str, Any] = {
    "org": "acme",
    "namespace": "repo-x",
    "owner_email": "alice",
    "contact_email": "alice@acme.io",
    "password_hash": "$argon2id$…",
    "kind": "code",
    "classification": "standard",
    "join_policy": "approval",
    "retention_days": 90,
    "active": True,
    "created_at": datetime(2026, 1, 1),
    "updated_at": datetime(2026, 1, 2),
}


class _CH:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.inserts: dict[str, list[dict[str, Any]]] = {}

    async def query_one(self, sql: str, params: Any = None) -> Any:
        return dict(self.row) if self.row else None

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "sso_links" in sql:
            return [{"email": "alice@example.com"}]  # linked for SSO before the release
        if "machine_identities" in sql:
            return []  # no bot names the namespace
        return [{"subject": "alice", "role": "admin", "granted_by": "alice"}]

    async def insert(self, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
        self.inserts.setdefault(table, []).extend(rows)


class _Req:
    def __init__(self, token: str = "") -> None:
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(revocation, "_loaded", True)
    state = {"restricted": False, "allows": False}

    async def is_restricted(org: str, ns: str) -> bool:
        return bool(state["restricted"])

    async def allows(*a: Any) -> bool:
        return bool(state["allows"])

    monkeypatch.setattr(nc, "is_restricted", is_restricted)
    monkeypatch.setattr("server.auth.authz.restricted_allows", allows)

    def run(token: str, row: dict[str, Any] | None = _ROW, org: str = "acme") -> tuple[Any, _CH]:
        ch = _CH(row)
        monkeypatch.setattr(cr, "get_client", lambda: ch)
        inp = cr.ClaimReleaseInput(org=org, namespace="repo-x")
        return asyncio.run(cr.claim_release(inp, _Req(token))), ch  # type: ignore[arg-type]

    return run, state


def _tok(roles: list[str], org: str = "acme") -> str:
    return mint_token("op", org, ["*"], roles)


def _status(run: Any, *a: Any, **k: Any) -> int:
    with pytest.raises(HTTPException) as ei:
        run(*a, **k)
    return ei.value.status_code


def test_the_release_keeps_every_column_but_the_claim(env: Any) -> None:
    run, _ = env
    out, ch = run(_tok(["org_admin"]))
    assert out.released and out.previous_owner == "alice" and out.grants_revoked == 1
    # Its SSO link goes too: a next holder of the name is not who linked it.
    (link,) = ch.inserts["loom.sso_links"]
    assert (link["email"], link["linked"]) == ("alice@example.com", 0)
    (row,) = ch.inserts["loom.namespaces"]
    assert {k: row[k] for k in ("owner_email", "contact_email", "password_hash", "active")} == {
        "owner_email": "",
        "contact_email": "",
        "password_hash": "",
        "active": False,
    }
    for kept in ("kind", "classification", "join_policy", "retention_days", "created_at"):
        assert row[kept] == _ROW[kept], kept
    assert ch.inserts["loom.namespace_grants"][0]["revoked_at"] is not None


@pytest.mark.parametrize(
    ("token", "status"),
    [("", 401), ("not-a-jwt", 401), (None, 403), ("admin", 403)],
    ids=["no-bearer", "garbage", "writer", "namespace-admin"],
)
def test_only_an_org_admin_may_release(env: Any, token: str | None, status: int) -> None:
    run, _ = env
    tok = {None: _tok(["writer"]), "admin": _tok(["admin"])}.get(token, token)
    assert _status(run, tok) == status


def test_an_org_admin_of_another_org_is_refused(env: Any) -> None:
    run, _ = env
    assert _status(run, _tok(["org_admin"], org="other")) == 403


def test_a_restricted_namespace_is_answered_like_a_missing_one(env: Any) -> None:
    run, state = env
    state["restricted"] = True
    assert _status(run, _tok(["platform_admin"])) == 404
    assert _status(run, _tok(["platform_admin"]), row=None) == 404
    state["allows"] = True  # a direct admin grant + a claim naming it
    assert run(_tok(["org_admin"]))[0].released


def test_auth_off_releases_as_local(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = env
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    _, ch = run("")
    assert ch.inserts["loom.namespaces"][0]["description"].startswith("Released by local")


def test_a_release_ends_the_tokens_of_the_bots_and_agent_it_reached(monkeypatch) -> None:
    from server.auth import machines
    from server.identity.app import account_admin

    ended: list[str] = []

    async def forget(org: str, ns: str, by: str, *, ch: Any) -> list[str]:
        return ["triage"]

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        ended.append(subject)

    monkeypatch.setattr(machines, "forget_namespace", forget)
    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    row = {"tags": ["machine:fleet"], "owner_email": "sa:fleet:a1"}
    asyncio.run(cr._forget_machines("acme", "fleet-a1", row, "root", ch=None))
    assert ended == ["sa:triage", "sa:fleet:a1"]
