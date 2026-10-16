"""Bots and CI jobs at /oauth/token: a key's client assertion (RFC 7523) and a
CI job's OIDC token (RFC 8693) each get an hour-long service-account token,
once, and nothing else does."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from server.auth import machines, oauth, oidc, revocation
from tests.unit.oauth_store_fake import MemoryOAuthStore, oauth_app

pytestmark = pytest.mark.filterwarnings("default")
ENDPOINT = "http://testserver/oauth/token"
CI = "https://ci.example"


@pytest.fixture
def bots(monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(oauth.settings, "oauth_issuer", "http://testserver")
    private, public = machines.generate_key()
    ci_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    registered: dict[str, machines.Machine] = {
        "triage": {
            "name": "triage",
            "org": "acme",
            "ns_roles": [("triage", "writer"), ("docs", "viewer")],
            "public_keys": machines.public_jwks(public),
            "issuer": "",
            "audience": "",
            "claims": {},
        },
        "deploy": {
            "name": "deploy",
            "org": "acme",
            "ns_roles": [("deploys", "writer")],
            "public_keys": "",
            "issuer": CI,
            "audience": "loom",
            "claims": {"repository": "acme/app", "ref": "refs/heads/*"},
        },
    }

    async def get(name: str) -> machines.Machine | None:
        return registered.get(name)

    async def jwks_uri(issuer: str) -> str:
        return f"{issuer}/keys"

    async def emit(event_type: str, **kw: Any) -> str:
        return ""

    async def not_revoked(subjects: list[str], *, issued_at: int) -> bool:
        return False

    monkeypatch.setattr(machines, "get", get)
    monkeypatch.setattr(machines, "_jwks_uri", jwks_uri)
    monkeypatch.setattr(oidc, "signing_key", lambda uri, token: ci_key.public_key())
    monkeypatch.setattr(oauth.audit, "emit", emit)
    monkeypatch.setattr(revocation, "subjects_revoked", not_revoked)
    return {"key": private, "ci_key": ci_key, "registered": registered}


def _assertion(key: Any, **over: Any) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    now = int(time.time())
    claims = {"iss": "triage", "sub": "triage", "aud": ENDPOINT, "iat": now, "exp": now + 60}
    claims |= {"jti": uuid.uuid4().hex, **over}
    signer = load_pem_private_key(key, password=None) if isinstance(key, bytes) else key
    return pyjwt.encode(claims, signer, algorithm="ES256")


def _with_key(c: TestClient, assertion: str, client_id: str = "triage") -> Any:
    return c.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_assertion_type": machines.ASSERTION_TYPE,
            "client_assertion": assertion,
        },
    )


def test_a_bots_key_gets_an_hour_long_scoped_token(bots) -> None:
    r = _with_key(TestClient(oauth_app()), _assertion(bots["key"]))
    assert r.status_code == 200 and r.json()["expires_in"] == 3600
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "sa:triage" and claims["namespaces"] == ["triage", "docs"]
    assert claims["exp"] - claims["iat"] == 3600 and "refresh_token" not in r.json()


def test_an_assertion_is_spent_once(bots) -> None:
    c, assertion = TestClient(oauth_app()), _assertion(bots["key"])
    assert _with_key(c, assertion).status_code == 200
    assert _with_key(c, assertion).status_code == 401


@pytest.mark.parametrize(
    "over",
    [
        {"aud": "https://elsewhere/oauth/token"},  # made for another server
        {"exp": int(time.time()) + 3600},  # kept, not made for one request
        {"sub": "admin"},
        {"iss": "deploy", "sub": "deploy"},  # a bot with no key
    ],
)
def test_an_assertion_off_its_shape_is_refused(bots, over: dict[str, Any]) -> None:
    r = _with_key(TestClient(oauth_app()), _assertion(bots["key"], **over), client_id="")
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_only_the_bots_own_key_signs_for_it(bots) -> None:
    other, _ = machines.generate_key()
    assert _with_key(TestClient(oauth_app()), _assertion(other)).status_code == 401


@pytest.mark.parametrize("alg", ["none", "HS256"])
def test_an_unsigned_or_shared_secret_assertion_is_refused(bots, alg: str) -> None:
    now = int(time.time())
    claims = {
        "iss": "triage",
        "sub": "triage",
        "aud": ENDPOINT,
        "iat": now,
        "exp": now + 60,
        "jti": "j",
    }
    forged = pyjwt.encode(claims, "s" * 32 if alg == "HS256" else None, algorithm=alg)
    assert _with_key(TestClient(oauth_app()), forged).status_code == 401


def test_a_disabled_bot_gets_nothing(bots) -> None:
    del bots["registered"]["triage"]  # machines.get answers only active ones
    assert _with_key(TestClient(oauth_app()), _assertion(bots["key"])).status_code == 401


def _ci_token(bots: dict[str, Any], **over: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": CI,
        "aud": "loom",
        "sub": "repo:acme/app:ref:refs/heads/main",
        "repository": "acme/app",
        "ref": "refs/heads/main",
        "iat": now,
        "exp": now + 300,
        "jti": uuid.uuid4().hex,
    }
    return pyjwt.encode(claims | over, bots["ci_key"], algorithm="RS256")


def _exchange(c: TestClient, token: str, client_id: str = "deploy") -> Any:
    return c.post(
        "/oauth/token",
        data={
            "grant_type": machines.TOKEN_EXCHANGE,
            "client_id": client_id,
            "subject_token": token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        },
    )


def test_a_ci_jobs_token_gets_tokens_that_never_outlive_it(bots) -> None:
    c, token = TestClient(oauth_app()), _ci_token(bots)  # lives five minutes
    r = _exchange(c, token)
    assert r.status_code == 200 and 240 <= r.json()["expires_in"] <= 300
    assert r.json()["issued_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "sa:deploy" and claims["namespaces"] == ["deploys"]
    assert claims["exp"] <= pyjwt.decode(token, options={"verify_signature": False})["exp"]
    # Another client of the same job, or a renewal, exchanges it again.
    assert _exchange(c, token).status_code == 200


@pytest.mark.parametrize(
    "over",
    [
        {"repository": "evil/app"},  # another repository on the same platform
        {"ref": "refs/tags/v1"},
        {"aud": "someone-else"},
        {"iss": "https://other-ci.example"},
    ],
)
def test_a_ci_token_off_the_trust_is_refused(bots, over: dict[str, Any]) -> None:
    r = _exchange(TestClient(oauth_app()), _ci_token(bots, **over))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_a_ci_token_signed_by_another_key_is_refused(bots) -> None:
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = pyjwt.encode(
        {
            "iss": CI,
            "aud": "loom",
            "sub": "x",
            "repository": "acme/app",
            "ref": "refs/heads/main",
            "iat": now,
            "exp": now + 300,
        },
        stranger,
        algorithm="RS256",
    )
    assert _exchange(TestClient(oauth_app()), forged).status_code == 400


def test_a_bot_without_a_trust_takes_no_ci_token(bots) -> None:
    assert (
        _exchange(TestClient(oauth_app()), _ci_token(bots), client_id="triage").status_code == 401
    )


def test_metadata_names_both_machine_grants() -> None:
    meta = oauth.authorization_server_metadata("https://loom.example")
    assert {"client_credentials", machines.TOKEN_EXCHANGE} <= set(meta["grant_types_supported"])
    assert "private_key_jwt" in meta["token_endpoint_auth_methods_supported"]


async def test_a_saved_bot_writes_every_column(monkeypatch) -> None:
    from tests.unit.schema_ddl import column_names

    rows: list[dict[str, Any]] = []

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"v": max((r["version"] for r in rows), default=0)}

        async def insert(self, table: str, batch: list[dict[str, Any]]) -> None:
            assert table == "loom.machine_identities"
            rows.extend(batch)

    monkeypatch.setattr(machines, "get_client", lambda: _CH())
    bot: machines.Machine = {
        "name": "triage",
        "org": "acme",
        "ns_roles": [("triage", "writer")],
        "public_keys": "",
        "issuer": "",
        "audience": "",
        "claims": {},
    }
    await machines.save(bot, by="cli:root")
    await machines.save(bot, by="cli:root", active=False)
    assert [sorted(r) for r in rows] == [sorted(column_names("machine_identities"))] * 2
    assert rows[1]["version"] > rows[0]["version"]


def test_a_bot_disabled_while_its_token_is_made_gets_a_dead_one(bots, monkeypatch) -> None:
    # The disable lands just after the proof read the bot as active: its
    # cutoff is a later second, and the token is issued as of before the read.
    read = machines.get

    async def read_then_disabled(name: str) -> machines.Machine | None:
        found = await read(name)
        revocation._apply_locally(machines.subject(name), int(time.time()) + 1)
        await asyncio.sleep(1.1)  # the request waits past the disable's cutoff
        return found

    async def subjects_revoked(subjects: list[str], *, issued_at: int) -> bool:
        return False  # the check ran before the disable

    monkeypatch.setattr(revocation, "_loaded", True)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {})
    monkeypatch.setattr(machines, "get", read_then_disabled)
    monkeypatch.setattr(revocation, "subjects_revoked", subjects_revoked)
    r = _with_key(TestClient(oauth_app()), _assertion(bots["key"]))
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert revocation.is_revoked(claims["sub"], issued_at=claims["iat"])


def test_two_clients_of_one_job_share_its_token_file(bots, monkeypatch, tmp_path) -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))
    from loom import machine

    server = TestClient(oauth_app())
    monkeypatch.setattr(
        machine.httpx,
        "post",
        lambda url, *, data, timeout: server.post(url.removeprefix("http://testserver"), data=data),
    )
    (tmp_path / "oidc").write_text(_ci_token(bots))  # the platform wrote it once
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN_FILE", str(tmp_path / "oidc"))
    monkeypatch.delenv("LOOM_WORKLOAD_TOKEN", raising=False)
    first, second = (machine.MachineLogin("http://testserver", "deploy") for _ in range(2))
    assert first.token() and second.token()
    first._renew_at = 0  # a renewal, from the unchanged file
    assert first.token(refused=first.token())


def test_an_issuer_ending_in_a_slash_takes_its_own_tokens(bots) -> None:
    bots["registered"]["deploy"]["issuer"] = "https://sts.example/tenant/"
    token = _ci_token(bots, iss="https://sts.example/tenant/")
    assert _exchange(TestClient(oauth_app()), token).status_code == 200


def test_a_ci_token_at_its_end_gets_nothing(bots) -> None:
    # Inside the verify's leeway it still decodes; a token issued now would outlive it.
    token = _ci_token(bots, exp=int(time.time()) - 10)
    r = _exchange(TestClient(oauth_app()), token)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_a_trust_whose_discovery_names_another_issuer_is_refused_for_good(monkeypatch) -> None:
    import httpx

    class _Web:
        async def get(self, url: str, **kw: Any) -> httpx.Response:
            doc = {"issuer": "https://elsewhere.example", "jwks_uri": "https://x/keys"}
            return httpx.Response(200, json=doc, request=httpx.Request("GET", url))

    from server.clients import _http

    machines._discovered.clear()
    monkeypatch.setattr(_http, "get_client", lambda: _Web())
    with pytest.raises(machines.Refused) as refused:
        asyncio.run(machines._jwks_uri("https://ci.example"))
    assert refused.value.error == "invalid_client" and "_Undiscovered" not in str(refused.value)
