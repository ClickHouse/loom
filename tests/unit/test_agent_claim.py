"""A person claims an agent's namespace with the code its sign-up returned: the
code never signs in, it claims once, and the claim leaves the person's own
password as the namespace's only one. Hermetic: ClickHouse is faked."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server.auth.self_signup as ss
from server.auth import login, oauth, passwords
from server.identity.app import agent_claim

_NS = "agent-nightly-triage"


class _Req:
    headers = {"host": "loom.test:7777"}
    client = type("C", (), {"host": "10.0.0.1"})()
    url = type("U", (), {"scheme": "http"})()


class _SignupCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql: str, params: Any = None) -> None:
        return None

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


@pytest.fixture(autouse=True)
def _claims_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.claims_fake import memory_claims

    memory_claims(monkeypatch)


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, auth_attempts: Any) -> dict[str, Any]:
    """An agent signed up through the real route; its namespace row is what the
    claim page and the sign-in read, newest version first as under FINAL."""
    from server.settings import settings

    monkeypatch.setattr(settings, "self_signup", "open")
    monkeypatch.setattr(settings, "oauth_login_provider", "password")
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    signup_ch = _SignupCH()
    monkeypatch.setattr(ss, "get_client", lambda: signup_ch)
    out = asyncio.run(ss.agent_signup(ss.AgentSignupInput(name="nightly triage"), _Req()))
    [row] = signup_ch.inserts[0][1]
    row = {**row, "updated_at": datetime(2026, 1, 1)}
    w: dict[str, Any] = {"code": out.claim_code, "rows": [row], "written": []}

    def newest() -> dict[str, Any]:
        return dict(w["rows"][-1])

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            r = newest()
            if (params or {}).get("ident", (params or {}).get("ns")) != r["namespace"]:
                return []
            if not r["active"] or not r["password_hash"]:
                return []
            claim_row = r["password_hash"].startswith(passwords.CLAIM_PREFIX)
            if "startsWith(password_hash" in sql and ("NOT startsWith" in sql) == claim_row:
                return []
            return [r]

        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            assert table == "loom.namespaces"
            w["rows"].extend(rows)
            w["written"].extend(rows)

    async def fetch_row(org: str, namespace: str) -> dict[str, Any] | None:
        r = newest()
        return r if (r["org"], r["namespace"]) == (org, namespace) else None

    async def emit(event_type: str, **kw: Any) -> str:
        w.setdefault("events", []).append((event_type, kw))
        return ""

    async def no_restricted(subject: str, org: str, namespaces: list[str]) -> list[str]:
        return list(namespaces)

    monkeypatch.setattr(agent_claim, "get_client", lambda: _CH())
    monkeypatch.setattr(agent_claim.namespaces, "fetch_row", fetch_row)
    monkeypatch.setattr(agent_claim.audit, "emit", emit)
    monkeypatch.setattr(login, "get_client", lambda: _CH())
    monkeypatch.setattr(login, "widen_with_restricted_grants", no_restricted)
    return w


def _app() -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.add_exception_handler(oauth.OAuthError, oauth.oauth_error_response)
    app.include_router(agent_claim.router)
    return TestClient(app)


def _claim(c: TestClient, code: str, password: str, repeat: str | None = None) -> Any:
    return c.post(
        "/oauth/agent-claim",
        data={
            "namespace": _NS,
            "code": code,
            "password": password,
            "repeat": password if repeat is None else repeat,
        },
    )


def _sign_in(password: str) -> Any:
    return asyncio.run(login.resolve_user_credentials(_NS, password))


def test_the_code_claims_once_and_the_claimer_signs_in_with_their_password(world) -> None:
    c = _app()
    page = c.get("/oauth/agent-claim", params={"namespace": _NS})
    assert page.status_code == 200 and 'name="code"' in page.text and _NS in page.text

    r = _claim(c, world["code"], "correct horse")
    assert r.status_code == 200 and _NS in r.text
    [row] = world["written"]
    assert row["password_hash"].startswith("$argon2")
    assert passwords.verify_password(row["password_hash"], "correct horse")
    assert (row["owner_email"], row["active"]) == (_NS, True)
    [(event, kw)] = world["events"]
    assert (event, kw["namespace"], kw["actor"]) == ("auth.agent_claimed", _NS, _NS)

    # The code is spent: neither a second claim nor a sign-in takes it.
    assert _claim(c, world["code"], "attacker pass").status_code == 400
    assert len(world["written"]) == 1
    assert _sign_in(world["code"]) is None
    subject, org, namespaces, _ = _sign_in("correct horse")
    assert (subject, org, namespaces) == (_NS, "dev", [_NS])


def test_an_unclaimed_code_never_signs_in(world) -> None:
    assert _sign_in(world["code"]) is None
    assert not passwords.verify_password(world["rows"][0]["password_hash"], world["code"])


def test_a_wrong_code_or_a_bad_password_claims_nothing_and_keeps_the_code(world) -> None:
    c = _app()
    assert _claim(c, "not-the-code", "correct horse").status_code == 400
    assert _claim(c, world["code"], "short").status_code == 400
    assert _claim(c, world["code"], "correct horse", "correct hors").status_code == 400
    assert world["written"] == []
    assert _claim(c, world["code"], "correct horse").status_code == 200


def test_a_claim_that_lands_first_is_not_overwritten(world, monkeypatch) -> None:
    # The row is read again once the account is held: a claim (or a release)
    # that held it first changed the hash, and this one is refused.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def claimed_meanwhile(org: str, namespace: str) -> Any:
        world["rows"].append({**world["rows"][-1], "password_hash": "$argon2id$other"})
        yield

    monkeypatch.setattr(agent_claim, "account_change", claimed_meanwhile)
    assert _claim(_app(), world["code"], "correct horse").status_code == 400
    assert world["written"] == []


def test_guessing_codes_is_throttled_like_a_sign_in(world) -> None:
    c = _app()
    for _ in range(10):
        assert _claim(c, "not-the-code", "correct horse").status_code == 400
    assert _claim(c, world["code"], "correct horse").status_code == 429
    assert world["written"] == []


def test_no_claim_while_sso_is_on(world, monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "oidc")
    c = _app()
    assert c.get("/oauth/agent-claim").status_code == 400
    assert _claim(c, world["code"], "correct horse").status_code == 400
    assert world["written"] == []
