"""A person claims an agent's namespace with the code its sign-up returned: the
code never signs in (dashboard, OAuth authorize form, device grant), it claims
once, and the claim leaves the person's own password as the namespace's only
one. An agent account made before the code was stored prefixed is unclaimed by
its shape alone. The statements run on clickhouse-local against the real table."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server.auth.self_signup as ss
from server.auth import claim_codes, device, login, oauth, passwords
from server.identity.app import agent_claim
from server.identity.store import namespaces
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl
from tests.unit.oauth_store_fake import MemoryOAuthStore, oauth_app

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_NS = "agent-nightly-triage"
_LEGACY = "agent-old-bot"


class _Req:
    headers = {"host": "loom.test:7777"}
    client = type("C", (), {"host": "10.0.0.1"})()
    url = type("U", (), {"scheme": "http"})()


@pytest.fixture(autouse=True)
def _claims_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.claims_fake import memory_claims

    memory_claims(monkeypatch)


def _account(namespace: str, owner: str, email: str, password_hash: str, tags: str) -> str:
    return (
        "INSERT INTO loom.namespaces (org, namespace, owner_email, contact_email, password_hash,"
        f" tags, active, created_at, updated_at) VALUES ('dev', '{namespace}', '{owner}',"
        f" '{email}', '{password_hash}', {tags}, 1, now(), now())"
    )


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, auth_attempts: Any, tmp_path: Path) -> dict[str, Any]:
    """An agent signed up through the real route, one signed up before its code was
    stored prefixed (a plain hash), and a person whose account carries the agent
    tag and an email, all in one kept clickhouse-local database."""
    from server.settings import settings

    monkeypatch.setattr(settings, "self_signup", "open")
    monkeypatch.setattr(settings, "oauth_login_provider", "password")
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    ch = KeptCH(tmp_path / "ch", ddl("namespaces"), ddl("namespace_grants"))
    ch.tables.add("loom.service_accounts")  # written by the sign-up, read by no test here
    original_insert = ch.insert

    async def insert(table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if table != "loom.service_accounts":
            await original_insert(table, rows, settings)

    ch.insert = insert  # type: ignore[method-assign]
    for module in (ss, agent_claim, login):
        monkeypatch.setattr(module, "get_client", lambda: ch)
    monkeypatch.setattr(namespaces, "_default_client", lambda: ch)
    out = asyncio.run(ss.agent_signup(ss.AgentSignupInput(name="nightly triage"), _Req()))
    legacy_code = "legacy-claim-code"
    ch.create(
        _account(_LEGACY, _LEGACY, "", passwords.hash_password(legacy_code), "['agent']"),
        _account(
            "lee", "lee", "lee@acme.com", passwords.hash_password("lee's password"), "['agent']"
        ),
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, **kw: Any) -> str:
        events.append((event_type, kw))
        return ""

    async def no_restricted(subject: str, org: str, names: list[str]) -> list[str]:
        return list(names)

    hashed: list[str] = []
    real_hash = agent_claim.hash_password_off_loop

    async def counted_hash(password: str) -> str:
        hashed.append(password)
        return await real_hash(password)

    monkeypatch.setattr(agent_claim.audit, "emit", emit)
    monkeypatch.setattr(agent_claim, "hash_password_off_loop", counted_hash)
    monkeypatch.setattr(login, "widen_with_restricted_grants", no_restricted)
    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    return {
        "ch": ch,
        "code": out.claim_code,
        "legacy_code": legacy_code,
        "events": events,
        "hashed": hashed,
    }


def _app() -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.add_exception_handler(oauth.OAuthError, oauth.oauth_error_response)
    app.include_router(agent_claim.router)
    return TestClient(app)


def _claim(
    c: TestClient, code: str, password: str, repeat: str | None = None, namespace: str = _NS
) -> Any:
    return c.post(
        "/oauth/agent-claim",
        data={
            "namespace": namespace,
            "code": code,
            "password": password,
            "repeat": password if repeat is None else repeat,
        },
    )


def _sign_in(password: str, namespace: str = _NS) -> Any:
    return asyncio.run(login.resolve_user_credentials(namespace, password))


def _row(world: dict[str, Any], namespace: str) -> dict[str, Any]:
    row = asyncio.run(namespaces.fetch_row("dev", namespace))
    assert row is not None
    return row


def test_the_code_claims_once_and_the_claimer_signs_in_with_their_password(world) -> None:
    c = _app()
    page = c.get("/oauth/agent-claim", params={"namespace": _NS})
    assert page.status_code == 200 and 'name="code"' in page.text and _NS in page.text

    r = _claim(c, world["code"], "correct horse")
    assert r.status_code == 200 and _NS in r.text
    row = _row(world, _NS)
    assert row["password_hash"].startswith("$argon2")
    assert passwords.verify_password(row["password_hash"], "correct horse")
    assert (row["owner_email"], row["active"]) == (_NS, True)
    assert row["tags"] == ["agent", claim_codes.CLAIMED_TAG]
    [(event, kw)] = world["events"]
    assert (event, kw["namespace"], kw["actor"]) == ("auth.agent_claimed", _NS, _NS)

    # The code is spent: neither a second claim nor a sign-in takes it.
    assert _claim(c, world["code"], "attacker pass").status_code == 400
    assert passwords.verify_password(_row(world, _NS)["password_hash"], "correct horse")
    assert _sign_in(world["code"]) is None
    subject, org, names, _ = _sign_in("correct horse")
    assert (subject, org, names) == (_NS, "dev", [_NS])


def test_an_unclaimed_code_never_signs_in(world) -> None:
    assert _sign_in(world["code"]) is None
    assert not passwords.verify_password(_row(world, _NS)["password_hash"], world["code"])


def test_an_agent_made_before_the_prefix_is_claimed_once_by_its_plain_code(world) -> None:
    code = world["legacy_code"]
    assert _sign_in(code, _LEGACY) is None
    c = _app()
    assert _claim(c, code, "correct horse", namespace=_LEGACY).status_code == 200
    assert _sign_in(code, _LEGACY) is None
    assert _sign_in("correct horse", _LEGACY)[0] == _LEGACY
    assert _claim(c, code, "attacker pass", namespace=_LEGACY).status_code == 400


def test_a_person_with_an_email_is_no_unclaimed_agent(world) -> None:
    assert _sign_in("lee's password", "lee")[0] == "lee"
    assert _claim(_app(), "lee's password", "correct horse", namespace="lee").status_code == 400


def test_a_wrong_code_or_a_bad_password_claims_nothing_and_keeps_the_code(world) -> None:
    c = _app()
    assert _claim(c, "not-the-code", "correct horse").status_code == 400
    assert _claim(c, world["code"], "short").status_code == 400
    assert _claim(c, world["code"], "correct horse", "correct hors").status_code == 400
    assert _row(world, _NS)["password_hash"].startswith(claim_codes.CLAIM_PREFIX)
    assert _claim(c, world["code"], "correct horse").status_code == 200


def test_only_a_proven_code_costs_a_password_hash(world) -> None:
    c = _app()
    assert _claim(c, "anything", "correct horse", namespace="agent-unknown").status_code == 400
    assert _claim(c, "not-the-code", "correct horse").status_code == 400
    assert world["hashed"] == []
    for _ in range(9):
        _claim(c, "not-the-code", "correct horse")
    throttled = _claim(c, world["code"], "correct horse")
    assert throttled.status_code == 429 and throttled.headers["Retry-After"] == "900"
    assert world["hashed"] == []
    assert _row(world, _NS)["password_hash"].startswith(claim_codes.CLAIM_PREFIX)


def test_a_claim_that_lands_first_is_not_overwritten(world, monkeypatch) -> None:
    # The row is read again once the account is held: a claim that held it
    # first changed the hash, and this one is refused.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def claimed_meanwhile(org: str, namespace: str) -> Any:
        row = await namespaces.fetch_row("dev", _NS)
        assert row is not None
        await world["ch"].insert("loom.namespaces", [{**row, "password_hash": "$argon2id$other"}])
        yield

    monkeypatch.setattr(agent_claim, "account_change", claimed_meanwhile)
    assert _claim(_app(), world["code"], "correct horse").status_code == 400
    assert _row(world, _NS)["password_hash"] == "$argon2id$other"


def test_the_claimed_mark_survives_a_settings_write_and_no_form_sets_it(world) -> None:
    from datetime import datetime

    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    assert _claim(_app(), world["code"], "correct horse").status_code == 200
    now = datetime(2026, 1, 2)
    form = NamespaceRegisterInput(org="dev", namespace=_NS, owner_email=_NS, tags=["mine"])
    tags = registry_row(form, _row(world, _NS), now)["tags"]
    assert tags == ["mine", claim_codes.CLAIMED_TAG]
    forged = NamespaceRegisterInput(
        org="dev", namespace="x", owner_email="x", tags=[claim_codes.CLAIMED_TAG]
    )
    assert registry_row(forged, None, now)["tags"] == []


def test_no_claim_while_sso_is_on(world, monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "oidc")
    c = _app()
    assert c.get("/oauth/agent-claim").status_code == 400
    assert _claim(c, world["code"], "correct horse").status_code == 400
    assert _row(world, _NS)["password_hash"].startswith(claim_codes.CLAIM_PREFIX)


# ---- the other password forms take no claim code ----


def _authorize(c: TestClient, password: str) -> Any:
    redirect = "http://127.0.0.1:9999/cb"
    reg = c.post("/oauth/register", json={"redirect_uris": [redirect], "client_name": "t"}).json()
    verifier = "v" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return c.post(
        "/oauth/authorize",
        data={
            "client_id": reg["client_id"],
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "identifier": _NS,
            "password": password,
        },
        follow_redirects=False,
    )


def _device_sign_in(c: TestClient, password: str) -> Any:
    grant = c.post("/oauth/device_authorization", data={"client_id": device.DEVICE_CLIENT_ID})
    assert grant.status_code == 200, grant.text
    user_code = grant.json()["user_code"]
    return c.post(
        "/oauth/device", data={"user_code": user_code, "identifier": _NS, "password": password}
    )


def test_the_authorize_form_and_the_device_grant_refuse_a_claim_code(world, monkeypatch) -> None:
    async def emit(event_type: str, **kw: Any) -> str:
        return ""

    monkeypatch.setattr(device.audit, "emit", emit)
    c = TestClient(oauth_app())
    assert _authorize(c, world["code"]).status_code == 401
    assert _device_sign_in(c, world["code"]).status_code == 401
    # The same forms take the password the claim set.
    assert _claim(_app(), world["code"], "correct horse").status_code == 200
    assert _authorize(c, "correct horse").status_code in (302, 303)
    assert _device_sign_in(c, "correct horse").status_code == 200
