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
from server.identity.app import account_admin
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
    state: dict[str, Any] = {"restricted": False, "allows": False}

    async def is_restricted(org: str, ns: str) -> bool:
        return bool(state["restricted"])

    async def allows(*a: Any) -> bool:
        return bool(state["allows"])

    monkeypatch.setattr(nc, "is_restricted", is_restricted)
    monkeypatch.setattr("server.auth.authz.restricted_allows", allows)
    waited: list[datetime] = []

    async def wait(cutoff: datetime) -> None:
        waited.append(cutoff)

    monkeypatch.setattr(account_admin, "_wait_until", wait)
    state["waited"] = waited

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

    ended: list[str] = []

    async def naming(org: str, ns: str, *, ch: Any) -> list[str]:
        return ["triage"]

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        ended.append(subject)

    monkeypatch.setattr(machines, "naming", naming)
    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    row = {"tags": ["machine:fleet"], "owner_email": "sa:fleet:a1"}
    asyncio.run(cr._end_tokens("acme", "fleet-a1", row, "root", ch=None))  # type: ignore[arg-type]
    assert ended == ["sa:triage", "sa:fleet:a1"]
    ended.clear()
    forged = {"tags": ["machine:fleet"], "owner_email": "lee"}  # a person's, marked by hand
    asyncio.run(cr._end_tokens("acme", "lee", forged, "root", ch=None))  # type: ignore[arg-type]
    assert ended == ["sa:triage"]  # never the person's own sessions


def test_a_release_whose_cutoff_fails_keeps_what_a_retry_needs(env: Any, monkeypatch) -> None:
    # The row names the agent and the bots name the namespace: rewritten before
    # the cutoffs landed, a retry could no longer find whose tokens to end.
    from server.auth import machines

    run, _ = env
    forgotten: list[str] = []

    async def naming(org: str, ns: str, *, ch: Any) -> list[str]:
        return ["triage"]

    async def forget(org: str, ns: str, by: str, *, ch: Any) -> list[str]:
        forgotten.append(ns)
        return ["triage"]

    async def down(subject: str, *, by: str, reason: str) -> Any:
        raise RuntimeError("ClickHouse away")

    monkeypatch.setattr(machines, "naming", naming)
    monkeypatch.setattr(machines, "forget_namespace", forget)
    monkeypatch.setattr(account_admin, "end_tokens", down)
    with pytest.raises(RuntimeError):
        run(_tok(["org_admin"]))
    assert forgotten == []  # the bots still name it
    ended: list[str] = []

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        ended.append(subject)

    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    out, _ = run(_tok(["org_admin"]))
    assert out.released and forgotten == ["repo-x"]
    # Once before the row and bots change, once after, so no bot exchange in
    # between outlives the release.
    assert ended == ["sa:triage", "sa:triage"]


def test_a_failed_second_cutoff_is_logged_and_the_release_stands(
    env: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Once the bots are forgotten a retry finds nothing to end, so the release
    # reports done, names the bots for a hand revoke and waits out the first cutoff.
    from server.auth import machines

    run, state = env
    first = datetime(2026, 1, 3)
    forgotten: list[str] = []
    invalidated: list[tuple[str, Any]] = []

    async def naming(org: str, ns: str, *, ch: Any) -> list[str]:
        return ["triage"]

    async def forget(org: str, ns: str, by: str, *, ch: Any) -> list[str]:
        forgotten.append(ns)
        return ["triage"]

    async def end_tokens(subject: str, *, by: str, reason: str) -> datetime:
        if forgotten:
            raise RuntimeError("ClickHouse away")
        return first

    monkeypatch.setattr(machines, "naming", naming)
    monkeypatch.setattr(machines, "forget_namespace", forget)
    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)

    def invalidate(org: str, subjects: Any = None) -> None:
        invalidated.append((org, subjects))

    monkeypatch.setattr(cr.grant_cache, "invalidate", invalidate)
    with caplog.at_level("WARNING", logger=cr.__name__):
        out, _ = run(_tok(["org_admin"]))
    assert out.released
    assert "sa:triage" in caplog.text
    assert invalidated[-1] == ("acme", None)  # the whole org, not only the grantees
    assert state["waited"] == [first]
