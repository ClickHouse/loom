"""Self-signup confers no authority the caller merely types.

The signup email is unverified, the org and the namespace name are the
caller's input: none of them may become platform_admin, another tenant, or a
subject someone else's grants name. Hermetic: ClickHouse is faked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

import server.auth.self_signup as ss
from server.auth import login, revocation
from server.auth.jwt import verify_token
from server.operations.base import Role
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


class _CH:
    def __init__(self) -> None:
        self.inserts: list[str] = []

    async def query_one(self, sql: str, params: Any = None) -> Any:
        return {"n": 0} if "count()" in sql else None

    async def insert(self, table: str, *a: Any, **k: Any) -> None:
        self.inserts.append(table)


@pytest.fixture()
def ch(monkeypatch: pytest.MonkeyPatch, auth_attempts: Any) -> _CH:
    from server.settings import settings

    monkeypatch.setattr(settings, "self_signup", "open")
    monkeypatch.setattr(settings, "oauth_login_provider", "password")
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    monkeypatch.setattr(settings, "platform_admins", "alice@example.com,opshost")
    monkeypatch.setattr(revocation, "_loaded", True)
    fake = _CH()
    monkeypatch.setattr(ss, "get_client", lambda: fake)
    return fake


class _Req:
    headers: dict[str, str] = {}
    client = type("C", (), {"host": "10.0.0.1"})()


def _signup(**kw: Any) -> Any:
    body = {"namespace": "eve", "email": "eve@evil.test", "password": "hunter2hunter2"} | kw
    return asyncio.run(ss.self_signup(ss.SelfSignupInput(**body), _Req()))  # type: ignore[arg-type]


def _refusal(**kw: Any) -> dict[str, Any]:
    with pytest.raises(HTTPException) as ei:
        _signup(**kw)
    detail: dict[str, Any] = ei.value.detail  # type: ignore[assignment]
    return {"status": ei.value.status_code, **detail}


def test_an_operator_email_typed_at_signup_is_not_platform_admin(ch: _CH) -> None:
    info = verify_token(_signup(email="alice@example.com").token)
    assert info.roles == (Role.WRITER,)


@pytest.mark.parametrize("name", ["opshost", "gateway"])
def test_a_name_the_settings_empower_cannot_be_claimed(
    monkeypatch: pytest.MonkeyPatch, ch: _CH, name: str
) -> None:
    """A platform-admin handle and a trusted caller (which may act as anyone)
    are powers by name; sign-up makes the typed name the subject, so the first
    stranger to type it would hold them. Refused like a taken name."""
    from server.settings import settings

    monkeypatch.setattr(settings, "trusted_callers", "Gateway, other")  # grants fold case
    got = _refusal(namespace=name)
    assert (got["status"], got["code"]) == (409, "NAMESPACE_CLAIMED")
    assert ch.inserts == []


def test_a_subject_granted_elsewhere_in_the_org_cannot_be_claimed(
    monkeypatch: pytest.MonkeyPatch, ch: _CH
) -> None:
    """A minted token's subject that owns no namespace of its name still holds
    grants; signing up under it would inherit them."""

    class _Granted(_CH):
        # (org, subject) as granted: `loom mint-token --subject Bob`, and a `carol` elsewhere
        grants = (("dev", "Bob"), ("other-org", "carol"))

        async def query_one(self, sql: str, params: Any = None) -> Any:
            if "namespace_grants" in sql:
                held = {(o, s.lower()) for o, s in self.grants}
                return {"held": 1} if (params["org"], params["name"]) in held else None
            return await super().query_one(sql, params)

    fake = _Granted()
    monkeypatch.setattr(ss, "get_client", lambda: fake)
    got = _refusal(namespace="bob")
    assert (got["status"], got["code"]) == (409, "NAMESPACE_CLAIMED")
    assert fake.inserts == []
    assert _signup(namespace="carol", email="carol@x.io").subject == "carol"


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_only_an_unrevoked_grant_reserves_a_name() -> None:
    """A revoked grant carries no power to inherit, so it frees its name; a pending
    join request, which an approval would make live, keeps it. The read, run on the
    production table, takes each grant row's latest version."""

    def grant(subject: str, version: int, revoked: str = "NULL", status: str = "active") -> str:
        return (
            "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_by,"
            f" status, revoked_at, version) VALUES ('dev', 'team', '{subject}', 'admin', 'ops',"
            f" '{status}', {revoked}, {version})"
        )

    setup = [
        ddl("namespace_grants"),
        grant("Dave", 1),
        grant("carol", 1),
        grant("carol", 2, "'2026-01-01 00:00:00'"),
        grant("erin", 1, status="pending"),
        ddl("namespaces"),
        # A first write by `Frank` registered `team-y` with Frank as its owner, no grant.
        "INSERT INTO loom.namespaces (org, namespace, owner_email, tags) VALUES"
        " ('dev', 'team-y', 'Frank', ['auto-registered'])",
        ddl("service_accounts"),
        # Gail's live token: its creator may list and revoke it.
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by, version)"
        " VALUES ('dev', 'ci', 'sa:ci-abc', 'Gail', 1)",
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by, revoked_at,"
        " version) VALUES ('dev', 'old', 'sa:old-abc', 'hal', '2026-01-01 00:00:00', 1)",
    ]

    class _Local:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            rows = run(setup, sql, params)
            return rows[0] if rows else None

    asyncio.run(ss._refuse_empowered(_Local(), "dev", "carol"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ss._refuse_empowered(_Local(), "dev", "dave"))
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException):
        asyncio.run(ss._refuse_empowered(_Local(), "dev", "erin"))
    with pytest.raises(HTTPException):  # a namespace's owner may rewrite it
        asyncio.run(ss._refuse_empowered(_Local(), "dev", "frank"))
    with pytest.raises(HTTPException):
        asyncio.run(ss._refuse_empowered(_Local(), "dev", "gail"))
    asyncio.run(ss._refuse_empowered(_Local(), "dev", "hal"))  # its token was revoked


@pytest.mark.parametrize("org", ["eve-org", "clickhouse"])
def test_signup_cannot_pick_another_org(ch: _CH, org: str) -> None:
    assert _refusal(org=org)["code"] == "SIGNUP_ORG_FIXED"
    assert ch.inserts == []


def test_the_signup_org_itself_may_be_named(ch: _CH) -> None:
    assert _signup(org="dev").org == "dev"


@pytest.mark.parametrize(
    "name", ["bob@corp.com", "sa:bot-abc123", "*", "Bob", "a b", "personal-bob"]
)
def test_a_namespace_that_could_spell_another_subject_is_refused(ch: _CH, name: str) -> None:
    got = _refusal(namespace=name)
    assert (got["status"], got["code"]) == (422, "VALIDATION_FAILED")
    assert ch.inserts == []


def test_agent_signup_cannot_claim_an_empowered_name(
    monkeypatch: pytest.MonkeyPatch, ch: _CH
) -> None:
    """A person later signs in to an agent's account under its name: the name
    `agent-<slug>` is held to the same rule as a person's sign-up."""
    from server.settings import settings

    monkeypatch.setattr(settings, "trusted_callers", "agent-bot")
    req = type("R", (), {"headers": {}, "client": type("C", (), {"host": "10.0.0.8"})()})()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ss.agent_signup(ss.AgentSignupInput(name="bot"), req))  # type: ignore[arg-type]
    assert (ei.value.status_code, ei.value.detail["code"]) == (409, "NAMESPACE_CLAIMED")  # type: ignore[index]
    assert ch.inserts == []


def test_agent_signup_cannot_claim_a_granted_name(monkeypatch: pytest.MonkeyPatch, ch: _CH) -> None:
    class _Granted(_CH):
        async def query_one(self, sql: str, params: Any = None) -> Any:
            if "namespace_grants" in sql:
                return {"held": 1} if params["name"] == "agent-bot" else None
            return await super().query_one(sql, params)

    fake = _Granted()
    monkeypatch.setattr(ss, "get_client", lambda: fake)
    req = type("R", (), {"headers": {}, "client": type("C", (), {"host": "10.0.0.7"})()})()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ss.agent_signup(ss.AgentSignupInput(name="bot"), req))  # type: ignore[arg-type]
    assert (ei.value.status_code, ei.value.detail["code"]) == (409, "NAMESPACE_CLAIMED")  # type: ignore[index]
    assert fake.inserts == []


def test_agent_signup_cannot_pick_another_org(ch: _CH) -> None:
    req = type("R", (), {"headers": {}, "client": type("C", (), {"host": "10.0.0.9"})()})()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ss.agent_signup(ss.AgentSignupInput(name="bot", org="other"), req))  # type: ignore[arg-type]
    assert ei.value.detail["code"] == "SIGNUP_ORG_FIXED"  # type: ignore[index]


@pytest.mark.parametrize(
    ("subject", "verified", "want"),
    [
        ("eve", "", ["writer"]),
        ("opshost", "", ["platform_admin"]),  # an account that existed before it was listed
        ("alice@example.com", "", ["writer"]),  # an email-shaped legacy subject
        ("u-alice-1a2b3c4d", "alice@example.com", ["platform_admin"]),  # OIDC-verified
        ("u-bob-1a2b3c4d", "bob@example.com", ["writer"]),
    ],
)
def test_platform_roles_trust_only_verified_emails(
    ch: _CH, subject: str, verified: str, want: list[str]
) -> None:
    assert login.platform_roles(subject, verified_email=verified) == want


def test_password_login_ignores_the_unverified_contact_email(ch: _CH) -> None:
    rows = [
        {
            "namespace": "eve",
            "owner_email": "eve",
            "org": "dev",
            "contact_email": "alice@example.com",
        }
    ]
    assert login._identity_from_rows(rows)[3] == ["writer"]


def test_human_signups_are_rate_limited_per_address(ch: _CH) -> None:
    for i in range(ss.HUMAN_SIGNUPS_PER_HOUR):
        _signup(namespace=f"user-{i}", email=f"u{i}@x.io")
    got = _refusal(namespace="one-too-many", email="late@x.io")
    assert (got["status"], got["code"]) == (429, "RATE_LIMITED")


def test_concurrent_claims_of_one_name_admit_one(monkeypatch: pytest.MonkeyPatch, ch: _CH) -> None:
    """The existence check and the insert run under one lock per name, so of
    two racing claims only the first provisions and the second sees it."""
    claimed: set[str] = set()

    class _RacyCH(_CH):
        async def query_one(self, sql: str, params: Any = None) -> Any:
            if "count()" in sql:
                return {"n": 0}
            found = {"owner": "x"} if params and params.get("ns") in claimed else None
            await asyncio.sleep(0)  # the other claim runs between this check and our insert
            return found

        async def insert(self, table: str, rows: Any, **k: Any) -> None:
            if table == "loom.namespaces":
                claimed.add(rows[0]["namespace"])

    monkeypatch.setattr(ss, "get_client", lambda: _RacyCH())

    async def both() -> list[Any]:
        body = {"namespace": "same", "email": "a@x.io", "password": "hunter2hunter2"}
        return await asyncio.gather(
            ss.self_signup(ss.SelfSignupInput(**body), _Req()),  # type: ignore[arg-type]
            ss.self_signup(ss.SelfSignupInput(**body | {"email": "b@x.io"}), _Req()),  # type: ignore[arg-type]
            return_exceptions=True,
        )

    results = asyncio.run(both())
    assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in results) == 1


def test_concurrent_claims_of_one_email_admit_one(monkeypatch: pytest.MonkeyPatch, ch: _CH) -> None:
    """Two names, one email: the EMAIL_IN_USE check and the insert run under
    the email's claim, so the second sign-up is refused."""
    emails: set[str] = set()

    class _RacyCH(_CH):
        async def query_one(self, sql: str, params: Any = None) -> Any:
            if "count()" in sql:
                n = 1 if params and params.get("ce") in emails else 0
                await asyncio.sleep(0)  # the other claim runs between this check and our insert
                return {"n": n}
            return None

        async def insert(self, table: str, rows: Any, **k: Any) -> None:
            if table == "loom.namespaces":
                emails.add(rows[0]["contact_email"])

    monkeypatch.setattr(ss, "get_client", lambda: _RacyCH())

    async def both() -> list[Any]:
        body = {"namespace": "first", "email": "same@x.io", "password": "hunter2hunter2"}
        return await asyncio.gather(
            ss.self_signup(ss.SelfSignupInput(**body), _Req()),  # type: ignore[arg-type]
            ss.self_signup(ss.SelfSignupInput(**body | {"namespace": "second"}), _Req()),  # type: ignore[arg-type]
            return_exceptions=True,
        )

    results = asyncio.run(both())
    refused = [r for r in results if isinstance(r, HTTPException)]
    # The second claim finds the email's claim held, or the first account.
    assert len(refused) == 1
    assert refused[0].detail["code"] in ("SIGNUP_IN_PROGRESS", "EMAIL_IN_USE")  # type: ignore[index]


@pytest.fixture(autouse=True)
def _claims_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment-wide claims, without a lock table in the fake."""
    from tests.unit.claims_fake import memory_claims

    memory_claims(monkeypatch)
