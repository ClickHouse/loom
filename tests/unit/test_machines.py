"""Bots and CI jobs at /oauth/token: a key's client assertion (RFC 7523) and a
CI job's OIDC token (RFC 8693) each get a service-account token (an hour, or
the `expires_in` asked up to the bot's maximum), once, and nothing else does."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from server.auth import agents, attempts, machines, oauth, oidc, revocation
from server.core import grant_cache
from server.core.errors import BackendUnavailableError
from tests.unit.oauth_store_fake import MemoryOAuthStore, oauth_app

pytestmark = pytest.mark.filterwarnings("default")
ENDPOINT = "http://testserver/oauth/token"
CI = "https://ci.example"


@pytest.fixture
def bots(monkeypatch, auth_attempts) -> dict[str, Any]:
    # Exchange tests run after every replica protects human-admin credentials.
    async def live(*a, **kw):
        return [{"schema_version": 76}]

    monkeypatch.setattr("server.core.instances.read_live", live)
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
        "fleet": {
            "name": "fleet",
            "org": "acme",
            "ns_roles": [("docs", "viewer")],
            "public_keys": "",
            "issuer": CI,
            "audience": "loom",
            "claims": {"repository": "acme/app"},
            "agent_claim": "agent_id",
        },
    }
    for m in registered.values():
        m.setdefault("agent_claim", "")
        m.setdefault("jwks_uri", "")
        m.setdefault("max_token_s", 3600)
        m.setdefault("max_agents", 1000)
        m.setdefault("max_access_token_s", 3600)
        m.setdefault("platform_admin", False)
    made: list[tuple[str, str, str]] = []

    async def ensure_namespace(m: machines.Machine, agent: str) -> str:
        made.append((m["name"], agent))
        return f"{m['name']}-{agent}"

    async def get(name: str) -> machines.Machine | None:
        return registered.get(name)

    async def jwks_uri(issuer: str) -> str:
        return f"{issuer}/keys"

    async def emit(event_type: str, **kw: Any) -> str:
        return ""

    async def not_revoked(subjects: list[str], *, issued_at: int) -> bool:
        return False

    active = {"triage", "docs", "deploys"}

    async def live_roles(m: machines.Machine) -> list[tuple[str, str]]:
        return [(ns, role) for ns, role in m["ns_roles"] if ns in active]

    # Each subject's grant ranks, as `grant_cache.grant_map` reads them.
    grants: dict[str, dict[str, int]] = {}
    read_grants: list[str] = []

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        read_grants.append(subject)
        return grant_cache.GrantMap(ranks=dict(grants.get(subject, {})))

    async def active_namespaces(org: str, names: list[str]) -> set[str]:
        return {ns for ns in names if ns in active}

    monkeypatch.setattr(machines, "get", get)
    monkeypatch.setattr(machines, "live_roles", live_roles)
    monkeypatch.setattr(machines, "active_namespaces", active_namespaces)
    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(agents, "ensure_namespace", ensure_namespace)
    monkeypatch.setattr(machines, "_jwks_uri", jwks_uri)
    monkeypatch.setattr(oidc, "signing_key", lambda uri, token: ci_key.public_key())
    monkeypatch.setattr(oauth.audit, "emit", emit)
    monkeypatch.setattr(revocation, "subjects_revoked", not_revoked)
    return {
        "key": private,
        "ci_key": ci_key,
        "registered": registered,
        "active": active,
        "made": made,
        "grants": grants,
        "read_grants": read_grants,
        "attempts": auth_attempts,
    }


def _assertion(key: Any, **over: Any) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    now = int(time.time())
    claims = {"iss": "triage", "sub": "triage", "aud": ENDPOINT, "iat": now, "exp": now + 60}
    claims |= {"jti": uuid.uuid4().hex, **over}
    signer = load_pem_private_key(key, password=None) if isinstance(key, bytes) else key
    return pyjwt.encode(claims, signer, algorithm="ES256")


def _with_key(
    c: TestClient, assertion: str, client_id: str = "triage", expires_in: str | None = None
) -> Any:
    asking = {} if expires_in is None else {"expires_in": expires_in}
    return c.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_assertion_type": machines.ASSERTION_TYPE,
            "client_assertion": assertion,
            **asking,
        },
    )


def test_a_bots_key_gets_an_hour_long_scoped_token(bots) -> None:
    r = _with_key(TestClient(oauth_app()), _assertion(bots["key"]))
    assert r.status_code == 200 and r.json()["expires_in"] == 3600
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "sa:triage" and claims["namespaces"] == ["triage", "docs"]
    assert claims["exp"] - claims["iat"] == 3600 and "refresh_token" not in r.json()


def test_a_slow_bot_lookup_does_not_lengthen_its_token(bots, monkeypatch) -> None:
    clock = [float(int(time.time()))]
    get = machines.get

    async def slow_get(name: str) -> machines.Machine | None:
        clock[0] += 2
        return await get(name)

    monkeypatch.setattr(machines, "get", slow_get)
    monkeypatch.setattr(oauth.time, "time", lambda: clock[0])
    r = _with_key(TestClient(oauth_app()), _assertion(bots["key"]))
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == machines.TOKEN_TTL
    assert r.json()["expires_in"] == claims["exp"] - int(clock[0])


def test_a_namespace_released_since_the_bot_was_made_grants_it_nothing(bots) -> None:
    c = TestClient(oauth_app())
    bots["active"].discard("docs")
    r = _with_key(c, _assertion(bots["key"]))
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["namespaces"] == ["triage"]
    bots["active"].discard("triage")
    assert _with_key(c, _assertion(bots["key"])).status_code == 401


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


def _exchange(
    c: TestClient, token: str, client_id: str = "deploy", expires_in: str | None = None
) -> Any:
    asking = {} if expires_in is None else {"expires_in": expires_in}
    return c.post(
        "/oauth/token",
        data={
            "grant_type": machines.TOKEN_EXCHANGE,
            "client_id": client_id,
            "subject_token": token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            **asking,
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


@pytest.mark.parametrize("platform_admin", [False, True])
async def test_a_saved_bot_writes_every_column(monkeypatch, platform_admin: bool) -> None:
    from tests.unit.schema_ddl import column_names

    rows: list[dict[str, Any]] = []

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
            # `next_version`'s read (its SQL runs in test_next_version_local_ch.py).
            return {"v": len(rows) + 1}

        async def insert(self, table: str, batch: list[dict[str, Any]]) -> None:
            assert table == "loom.machine_identities"
            rows.extend(batch)

    monkeypatch.setattr(machines, "get_client", lambda: _CH())
    bot: machines.Machine = {
        "name": "triage",
        "org": "acme",
        "ns_roles": [] if platform_admin else [("triage", "writer")],
        "public_keys": "",
        "issuer": "",
        "audience": "",
        "claims": {},
        "agent_claim": "",
        "jwks_uri": "",
        "max_token_s": 3600,
        "max_agents": 1000,
        "max_access_token_s": 86400,
        "platform_admin": platform_admin,
    }
    await machines.save(bot, by="cli:root")
    await machines.save(bot, by="cli:root", active=False)
    assert [sorted(r) for r in rows] == [sorted(column_names("machine_identities"))] * 2
    assert [r["version"] for r in rows] == [1, 2]  # what next_version answered
    assert all(r["platform_admin"] == int(platform_admin) for r in rows)
    assert all(r["max_access_token_s"] == 86400 for r in rows)
    assert machines._machine(rows[-1])["platform_admin"] is platform_admin
    assert machines._machine(rows[-1])["max_access_token_s"] == 86400


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


def test_a_ci_token_running_out_while_it_is_verified_gets_nothing(bots, monkeypatch) -> None:
    # The issuer's keys took 40 s: the proof has 20 s left once verified.
    clock = [time.time()]
    verify = machines.verify_workload

    async def slow_verify(token: str, name: str) -> Any:
        found = await verify(token, name)
        clock[0] += 40
        return found

    monkeypatch.setattr(machines, "verify_workload", slow_verify)
    monkeypatch.setattr(oauth.time, "time", lambda: clock[0])
    token = _ci_token(bots, exp=int(clock[0]) + 60)
    r = _exchange(TestClient(oauth_app()), token)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_a_slow_revocation_lookup_does_not_stretch_a_token_past_its_proof(
    bots, monkeypatch
) -> None:
    clock = [time.time()]

    async def slow_lookup(subjects: list[str], *, issued_at: int) -> bool:
        clock[0] += 10
        return False

    monkeypatch.setattr(oauth.time, "time", lambda: clock[0])
    monkeypatch.setattr(revocation, "subjects_revoked", slow_lookup)
    proof_exp = int(clock[0]) + 60
    r = _exchange(TestClient(oauth_app()), _ci_token(bots, exp=proof_exp))
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["exp"] <= proof_exp and r.json()["expires_in"] <= proof_exp - int(clock[0])


def test_a_slow_agent_namespace_does_not_lengthen_its_token(bots, monkeypatch) -> None:
    # Waiting on another replica's claim for the agent's namespace.
    clock = [float(int(time.time()))]
    ensure = agents.ensure_namespace

    async def slow_ensure(m: machines.Machine, agent: str) -> str:
        clock[0] += 10
        return await ensure(m, agent)

    bots["registered"]["fleet"]["max_token_s"] = 7200
    monkeypatch.setattr(agents, "ensure_namespace", slow_ensure)
    monkeypatch.setattr(oauth.time, "time", lambda: clock[0])
    now = int(clock[0])
    token = _ci_token(bots, agent_id="a1", iat=now, exp=now + 7200)
    r = _exchange(TestClient(oauth_app()), token, client_id="fleet")
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == machines.TOKEN_TTL
    assert r.json()["expires_in"] == machines.TOKEN_TTL - 10


def test_each_agent_gets_its_own_subject_and_namespace(bots) -> None:
    c = TestClient(oauth_app())
    token = _ci_token(bots, agent_id="a1")
    r = _exchange(c, token, client_id="fleet")
    assert r.status_code == 200
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "sa:fleet:a1"
    assert claims["ns_roles"] == {"fleet-a1": "writer", "docs": "viewer"}  # its own, then shared
    assert claims["parent_subjects"] == ["sa:fleet"]  # disabling the trust ends it
    assert bots["made"] == [("fleet", "a1")]


def _agent_roles(bots: dict[str, Any], agent: str = "a1") -> dict[str, str]:
    token = _ci_token(bots, agent_id=agent)
    r = _exchange(TestClient(oauth_app()), token, client_id="fleet")
    assert r.status_code == 200
    return pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})["ns_roles"]


def test_an_agent_reads_what_another_agent_lent_it_and_no_more(bots) -> None:
    bots["active"].update({"fleet-a1", "fleet-a2"})
    bots["grants"]["sa:fleet:a2"] = {"fleet-a1": 1, "fleet-a2": 3}  # a1 lent a2 its namespace
    bots["grants"]["sa:fleet:a1"] = {"fleet-a1": 3}  # only its owner grant
    assert _agent_roles(bots, "a2") == {
        "fleet-a2": "writer",
        "docs": "viewer",
        "fleet-a1": "viewer",
    }
    assert _agent_roles(bots, "a1") == {"fleet-a1": "writer", "docs": "viewer"}


def test_a_grant_counts_as_writer_at_most_and_raises_a_shared_role(bots) -> None:
    bots["active"].update({"fleet-a1", "ops"})
    bots["grants"]["sa:fleet:a1"] = {"ops": 3, "docs": 2}
    roles = _agent_roles(bots)
    assert roles["ops"] == "writer"  # an admin's grant: never re-shared by the agent
    assert roles["docs"] == "writer"  # above the trust's viewer


def test_a_revoked_grant_or_a_released_namespace_lends_nothing(bots) -> None:
    bots["active"].update({"fleet-a1", "fleet-a2"})
    bots["grants"]["sa:fleet:a1"] = {"fleet-a2": 0, "gone": 1}  # revoked; not active
    assert _agent_roles(bots) == {"fleet-a1": "writer", "docs": "viewer"}


def test_a_keyed_bot_takes_only_its_registered_roles(bots) -> None:
    r = _with_key(TestClient(oauth_app()), _assertion(bots["key"]))
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["namespaces"] == ["triage", "docs"] and bots["read_grants"] == []


def test_a_lower_grant_leaves_the_trusts_role_for_the_gate_to_cap(bots) -> None:
    bots["active"].add("fleet-a1")
    bots["registered"]["fleet"]["ns_roles"] = [("docs", "writer")]
    bots["grants"]["sa:fleet:a1"] = {"docs": 1}
    assert _agent_roles(bots)["docs"] == "writer"  # authz.claim_cap reads it as viewer


def test_an_agents_token_names_lent_namespaces_by_name_until_the_next_would_not_fit(bots) -> None:
    lent = [f"ns-{i:03}-" + "x" * 57 for i in range(30)]  # 64 bytes: 143 in the token
    bots["active"].update({"fleet-a1", *lent, "zz"})
    bots["grants"]["sa:fleet:a1"] = {ns: 1 for ns in lent} | {"docs": 2, "zz": 1}
    roles = _agent_roles(bots)
    own = agents.names_bytes(["fleet-a1", "docs"])  # its own and the trust's
    fit = (agents.TOKEN_NAMESPACE_BYTES - own) // 143
    assert [ns for ns in roles if ns.startswith("ns-")] == lent[:fit]  # first by name
    assert "zz" not in roles  # short, but after the first that did not fit
    assert roles["docs"] == "writer"  # a raised role is never left out


_LONGEST = [f"l{i:03}" + "x" * 124 for i in range(30)]  # the longest names a namespace may have


@pytest.mark.parametrize(
    "trust, lent",
    [
        ([], _LONGEST),
        ([], [f"{i:03}" for i in range(600)]),  # three-byte names: the per-name overhead dominates
        ([f"t{i:03}" + "x" * 124 for i in range(12)], _LONGEST),  # the trust's own fill most of it
    ],
    ids=["longest", "short", "trust-and-lent"],
)
def test_an_agents_token_stays_under_a_proxys_header_limit(
    bots, trust: list[str], lent: list[str]
) -> None:
    bots["registered"]["fleet"]["ns_roles"] = [(ns, "viewer") for ns in trust]
    bots["active"].update({"fleet-a1", *trust, *lent})
    bots["grants"]["sa:fleet:a1"] = {ns: 2 for ns in lent}
    r = _exchange(TestClient(oauth_app()), _ci_token(bots, agent_id="a1"), client_id="fleet")
    token = r.json()["access_token"]
    claims = pyjwt.decode(token, options={"verify_signature": False})
    named = claims["namespaces"]
    kept = [ns for ns in named if ns in set(lent)]
    used = agents.names_bytes(named)
    next_one = agents.token_bytes(lent[len(kept)])
    assert used <= agents.TOKEN_NAMESPACE_BYTES < used + next_one  # the budget is full
    # A 4096-bit key signs 683 base64 characters; the test key fewer, so count the difference.
    signature = len(token.rsplit(".", 1)[1])
    assert len(f"Authorization: Bearer {token}") + 683 - signature < 8192 - 1024


@pytest.mark.parametrize("agent", [None, "", "A1", "a/b", "-a", "a" * 64])
def test_a_token_naming_no_agent_loom_takes_is_refused(bots, agent: str | None) -> None:
    over = {} if agent is None else {"agent_id": agent}
    r = _exchange(TestClient(oauth_app()), _ci_token(bots, **over), client_id="fleet")
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert bots["made"] == []


def test_disabling_the_trust_ends_its_agents_tokens(bots) -> None:
    from server.auth import revocation
    from server.auth.jwt import TokenError, verify_token

    r = _exchange(TestClient(oauth_app()), _ci_token(bots, agent_id="a1"), client_id="fleet")
    token = r.json()["access_token"]
    assert verify_token(token).subject == "sa:fleet:a1"
    revocation._cut_off["sa:fleet"] = int(time.time()) + 1  # the cutoff a disable writes
    with pytest.raises(TokenError):
        verify_token(token)


def test_a_trust_with_its_keys_address_skips_discovery(bots, monkeypatch) -> None:
    # The issuer's discovery may be reachable only where it runs; its keys
    # are published elsewhere.
    fetched: list[str] = []

    async def no_discovery(issuer: str) -> str:
        raise AssertionError("discovery was asked")

    def signing_key(uri: str, token: str) -> Any:
        fetched.append(uri)
        return bots["ci_key"].public_key()

    bots["registered"]["fleet"]["jwks_uri"] = "https://keys.example/jwks.json"
    monkeypatch.setattr(machines, "_jwks_uri", no_discovery)
    monkeypatch.setattr(oidc, "signing_key", signing_key)
    r = _exchange(TestClient(oauth_app()), _ci_token(bots, agent_id="a1"), client_id="fleet")
    assert r.status_code == 200 and fetched == ["https://keys.example/jwks.json"]


def test_unreadable_keys_refuse_without_naming_their_address(bots, monkeypatch) -> None:
    """`/oauth/token` answers without a token: the keys' URL and the network error
    are logged, never quoted."""

    def signing_key(uri: str, token: str) -> Any:
        raise pyjwt.PyJWKClientConnectionError(
            f"Fail to fetch data from the url, err: {uri} refused (10.0.0.7:8443)"
        )

    bots["registered"]["fleet"]["jwks_uri"] = "https://keys.internal/jwks.json"
    monkeypatch.setattr(oidc, "signing_key", signing_key)
    r = _exchange(TestClient(oauth_app()), _ci_token(bots, agent_id="a1"), client_id="fleet")
    assert r.json()["error"] == "temporarily_unavailable"
    assert r.json()["error_description"] == "the issuer's keys could not be read; retry"


def test_a_release_rewrites_each_bot_under_its_claim(monkeypatch) -> None:
    # A disable or replace landing between the read and the save must not be
    # written back over (a disabled bot saved active again).
    from contextlib import asynccontextmanager

    from server.auth import grant_claims

    steps: list[str] = []
    bot = {"name": "triage", "ns_roles": [("team-a", "writer"), ("team-b", "viewer")]}

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return [{"name": "triage"}]

    @asynccontextmanager
    async def held(name: str) -> Any:
        steps.append(f"hold {name}")
        yield
        steps.append(f"release {name}")

    async def get(name: str, **kw: Any) -> Any:
        steps.append("read")
        return dict(bot)

    async def save(m: Any, **kw: Any) -> None:
        steps.append(f"write {m['ns_roles']}")

    monkeypatch.setattr(grant_claims, "bot_change", held)
    monkeypatch.setattr(machines, "get", get)
    monkeypatch.setattr(machines, "save", save)
    names = asyncio.run(machines.forget_namespace("acme", "team-a", "root", ch=_CH()))
    assert names == ["triage"]
    assert steps == ["hold triage", "read", "write [('team-b', 'viewer')]", "release triage"]


def test_a_control_planes_per_agent_token_is_taken_as_issued(bots, monkeypatch) -> None:
    # The shape a control plane signs per agent: ES256, sub naming tenant and
    # agent, UUID claims, a budget object Loom ignores, five minutes of life,
    # and keys published at their own address.
    from cryptography.hazmat.primitives.asymmetric import ec

    issuer, tenant, agent = (
        "https://cp.example/workload-identity",
        str(uuid.uuid4()),
        str(uuid.uuid4()),
    )
    key = ec.generate_private_key(ec.SECP256R1())
    bots["registered"]["tenant-a"] = {
        "name": "tenant-a",
        "org": "acme",
        "ns_roles": [],
        "public_keys": "",
        "issuer": issuer,
        "audience": "loom-dev",
        "claims": {"tenant_id": tenant},
        "agent_claim": "agent_id",
        "jwks_uri": "https://keys.example/jwks.json",
        "max_token_s": 3600,
        "max_agents": 1000,
        "max_access_token_s": 3600,
        "platform_admin": False,
    }
    monkeypatch.setattr(oidc, "signing_key", lambda uri, token: key.public_key())
    now = int(time.time())
    claims = {
        "iss": issuer,
        "sub": f"tenants/{tenant}/agents/{agent}",
        "aud": "loom-dev",
        "tenant_id": tenant,
        "agent_id": agent,
        "budgets": {"agent": "1000000000000"},
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + 300,
    }
    token = pyjwt.encode(claims, key, algorithm="ES256", headers={"kid": "k1"})
    r = _exchange(TestClient(oauth_app()), token, client_id="tenant-a")
    assert r.status_code == 200, r.text
    issued = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert issued["sub"] == f"sa:tenant-a:{agent}"
    assert issued["ns_roles"] == {f"tenant-a-{agent}": "writer"}
    assert 240 <= r.json()["expires_in"] <= 300  # never past the workload token
    other = pyjwt.encode({**claims, "tenant_id": str(uuid.uuid4())}, key, algorithm="ES256")
    assert _exchange(TestClient(oauth_app()), other, client_id="tenant-a").status_code == 400


def test_an_exchange_naming_no_client_finds_its_trust_by_issuer_and_claims(
    bots, monkeypatch
) -> None:
    # A client that sends only the grant, the subject token and its type.
    async def names_of(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(m) for m in bots["registered"].values() if m["issuer"] == params["iss"]]

    class _CH:
        query_all = staticmethod(names_of)

    monkeypatch.setattr(machines, "get_client", lambda: _CH())
    c = TestClient(oauth_app())

    def exchange(token: str) -> Any:
        return c.post(
            "/oauth/token",
            data={
                "grant_type": machines.TOKEN_EXCHANGE,
                "subject_token": token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "scope": "anything",  # what a generic exchanger may add; ignored
            },
        )

    # Both CI trusts are on one issuer; only `fleet` takes a token with agent_id.
    del bots["registered"]["deploy"]
    r = exchange(_ci_token(bots, agent_id="a1"))
    assert r.status_code == 200, r.text
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "sa:fleet:a1"
    bots["registered"]["fleet2"] = {**bots["registered"]["fleet"], "name": "fleet2"}
    r = exchange(_ci_token(bots, agent_id="a1"))
    assert r.status_code == 400 and "several trusts" in r.json()["error_description"]


def test_a_workload_token_living_longer_than_the_trust_allows_is_refused(bots) -> None:
    now = int(time.time())
    long_lived = _ci_token(bots, iat=now, exp=now + 7200)  # the trust takes an hour at most
    r = _exchange(TestClient(oauth_app()), long_lived)
    assert r.status_code == 400 and "at most 3600" in r.json()["error_description"]


def _client_less(bots: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    """The trusts as one issuer query sees them, and an exchange naming no client."""

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return [dict(m) for m in bots["registered"].values() if m["issuer"] == params["iss"]]

    monkeypatch.setattr(machines, "get_client", lambda: _CH())
    c = TestClient(oauth_app())

    def exchange(token: str) -> Any:
        return c.post(
            "/oauth/token",
            data={
                "grant_type": machines.TOKEN_EXCHANGE,
                "subject_token": token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            },
        )

    return exchange


def test_a_trust_whose_keys_are_away_does_not_refuse_one_that_takes_the_token(
    bots, monkeypatch
) -> None:
    exchange = _client_less(bots, monkeypatch)
    del bots["registered"]["deploy"]
    bots["registered"]["broken"] = {
        **bots["registered"]["fleet"],
        "name": "broken",
        "claims": {"repository": "acme/other"},
        "jwks_uri": "https://down.example/jwks.json",
    }
    real = oidc.signing_key

    def signing_key(uri: str, token: str) -> Any:
        if "down.example" in uri:
            raise pyjwt.PyJWKClientConnectionError("down")
        return real(uri, token)

    monkeypatch.setattr(oidc, "signing_key", signing_key)
    assert exchange(_ci_token(bots, agent_id="a1")).status_code == 200


def test_many_trusts_of_one_issuer_check_the_signature_once(bots, monkeypatch) -> None:
    exchange = _client_less(bots, monkeypatch)
    del bots["registered"]["deploy"]
    for i in range(200):  # one per tenant
        bots["registered"][f"t{i}"] = {
            **bots["registered"]["fleet"],
            "name": f"t{i}",
            "claims": {"tenant_id": f"t{i}"},
        }
    bots["registered"]["fleet"]["claims"] = {"tenant_id": "mine"}
    keys: list[str] = []
    real = oidc.signing_key
    monkeypatch.setattr(
        oidc, "signing_key", lambda uri, token: keys.append(uri) or real(uri, token)
    )
    assert exchange(_ci_token(bots, agent_id="a1", tenant_id="mine")).status_code == 200
    assert len(keys) == 1


@pytest.mark.parametrize("aud", [{"x": 1}, [["a"]], 7])
def test_a_crafted_audience_is_refused_not_a_server_error(bots, monkeypatch, aud: Any) -> None:
    exchange = _client_less(bots, monkeypatch)
    r = exchange(_ci_token(bots, aud=aud))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_a_trust_whose_keys_are_away_and_would_take_the_token_makes_it_wait(
    bots, monkeypatch
) -> None:
    # Its keys back, the token would meet two trusts and be refused: while they
    # are away it must not be minted for the one that answered.
    exchange = _client_less(bots, monkeypatch)
    del bots["registered"]["deploy"]
    bots["registered"]["twin"] = {
        **bots["registered"]["fleet"],
        "name": "twin",
        "jwks_uri": "https://down.example/jwks.json",
    }
    real = oidc.signing_key

    def signing_key(uri: str, token: str) -> Any:
        if "down.example" in uri:
            raise pyjwt.PyJWKClientConnectionError("down")
        return real(uri, token)

    monkeypatch.setattr(oidc, "signing_key", signing_key)
    assert exchange(_ci_token(bots, agent_id="a1")).status_code == 503


def test_a_token_no_trust_takes_learns_nothing_of_the_trusts(bots, monkeypatch) -> None:
    exchange = _client_less(bots, monkeypatch)
    r = exchange(_ci_token(bots, repository="acme/elsewhere"))
    assert r.status_code == 400
    assert r.json()["error_description"] == "no trust of this issuer takes the subject token"


def test_one_agent_over_its_exchange_limit_is_refused_alone(bots, monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_agent", 2)
    c, token = TestClient(oauth_app()), _ci_token(bots, agent_id="a1")
    assert [_exchange(c, token, "fleet").status_code for _ in range(2)] == [200, 200]
    r = _exchange(c, token, "fleet")
    assert r.status_code == 429 and r.json()["error"] == "temporarily_unavailable"
    assert r.headers["Retry-After"] == "60"
    assert bots["made"] == [("fleet", "a1")] * 2  # the refused one made nothing
    assert _exchange(c, _ci_token(bots, agent_id="a2"), "fleet").status_code == 200


def test_a_trust_without_agents_counts_only_against_the_trust(bots, monkeypatch) -> None:
    # Every pull request job of a repository shares one `sub`.
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_agent", 1)
    c = TestClient(oauth_app())
    assert [_exchange(c, _ci_token(bots)).status_code for _ in range(3)] == [200] * 3


def test_a_trust_over_its_exchange_limit_refuses_every_agent(bots, monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_trust", 2)
    c = TestClient(oauth_app())
    codes = [_exchange(c, _ci_token(bots, agent_id=f"a{i}"), "fleet").status_code for i in range(3)]
    assert codes == [200, 200, 429]
    assert _exchange(c, _ci_token(bots)).status_code == 200  # another trust counts its own


def test_an_agent_refused_at_its_own_limit_spends_none_of_the_trusts(bots, monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_trust", 3)
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_agent", 1)
    c, token = TestClient(oauth_app()), _ci_token(bots, agent_id="a1")
    assert [_exchange(c, token, "fleet").status_code for _ in range(5)] == [200] + [429] * 4
    codes = [_exchange(c, _ci_token(bots, agent_id=a), "fleet").status_code for a in "234"]
    assert codes == [200, 200, 429]


def test_an_exchange_this_process_is_too_busy_to_count_is_answered_busy(bots, monkeypatch) -> None:
    monkeypatch.setattr(attempts, "_semaphore", lambda: asyncio.Semaphore(0))
    monkeypatch.setattr(attempts, "_SLOT_WAIT_S", 0.01)
    r = _exchange(TestClient(oauth_app()), _ci_token(bots))
    assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"
    assert r.headers["Retry-After"] == "5"


def test_exchanges_age_out_of_the_limit(bots, monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_trust", 1)
    c = TestClient(oauth_app())
    assert [_exchange(c, _ci_token(bots)).status_code for _ in range(2)] == [200, 429]
    bots["attempts"].now += oauth._EXCHANGE_WINDOW_S + 1
    assert _exchange(c, _ci_token(bots)).status_code == 200


def test_a_refused_subject_token_spends_no_exchange(bots, monkeypatch) -> None:
    # Anyone can send a token naming a trust; only one its issuer signed counts.
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_trust", 1)
    c = TestClient(oauth_app())
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = pyjwt.decode(_ci_token(bots), options={"verify_signature": False})
    forged = pyjwt.encode(claims, stranger, algorithm="RS256")
    for token in [forged, _ci_token(bots, repository="evil/app")] * 3:
        assert _exchange(c, token).status_code == 400
    assert _exchange(c, _ci_token(bots)).status_code == 200


def test_overlapping_exchanges_refused_at_the_recount_spend_none_of_the_trusts(
    auth_attempts, monkeypatch
) -> None:
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_trust", 3)
    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_agent", 1)
    count = auth_attempts.query_all

    async def overlapping() -> list[Any]:
        # Three renewals of a1: each first count is read before any of them
        # records, so all three find room, all three record, and each recount
        # runs after all three have recorded.
        together, calls = asyncio.Barrier(3), 0

        async def held(sql: str, params: Any = None) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls <= 3:  # a first count
                rows = await count(sql, params)
                await together.wait()
                return rows
            if calls <= 6:  # a recount
                await together.wait()
            return await count(sql, params)

        monkeypatch.setattr(auth_attempts, "query_all", held)
        try:
            renewals = [oauth._admit_exchange("fleet", "a1") for _ in range(3)]
            return await asyncio.gather(*renewals, return_exceptions=True)
        finally:
            monkeypatch.setattr(auth_attempts, "query_all", count)

    answers = [200 if r is None else r.status_code for r in asyncio.run(overlapping())]
    # The first two recounts find a1 over and release their rows, so the last
    # finds room. The refused ones are charged to no gate.
    assert answers == [429, 429, 200]

    def admitted(agent: str) -> bool:
        try:
            asyncio.run(oauth._admit_exchange("fleet", agent))
        except oauth.OAuthError as e:
            assert e.status_code == 429
            return False
        return True

    # The trust's budget, less a1's one admitted renewal, is left for the others.
    assert [admitted(a) for a in ("a2", "a3", "a4")] == [True, True, False]


def test_a_release_that_fails_leaves_the_exchange_charged(auth_attempts, monkeypatch) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr(oauth.settings, "workload_exchanges_per_agent", 1)
    count, insert = auth_attempts.query_all, auth_attempts.insert
    # Another renewal of a1 that this one's first count missed (racing),
    # stamped near the window's start so it ages out first.
    other = {
        "kind": "oauth.exchange.agent",
        "key_sha256": attempts._digest("fleet:a1"),
        "at": datetime.fromtimestamp(auth_attempts.now - 590, UTC).replace(tzinfo=None),
    }
    asyncio.run(insert("loom.auth_attempts", [other]))
    first = True

    async def racing(sql: str, params: Any = None) -> list[dict[str, Any]]:
        nonlocal first
        if first:  # read before the other replica's row landed
            first = False
            return []
        return await count(sql, params)

    async def no_release(table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if any(r["kind"].endswith(".ok") for r in rows):
            raise BackendUnavailableError("ClickHouse away")
        await insert(table, rows, settings)

    monkeypatch.setattr(auth_attempts, "query_all", racing)
    monkeypatch.setattr(auth_attempts, "insert", no_release)
    with pytest.raises(BackendUnavailableError):  # refused at its recount, then unreleased
        asyncio.run(oauth._admit_exchange("fleet", "a1"))
    monkeypatch.setattr(auth_attempts, "insert", insert)
    # Once the other replica's row is out of the window, only this exchange's
    # unreleased row is left, and it still counts: a1's next one is refused.
    auth_attempts.now += 20
    with pytest.raises(oauth.OAuthError) as refused:
        asyncio.run(oauth._admit_exchange("fleet", "a1"))
    assert refused.value.status_code == 429


# ── a token's lifetime: `expires_in` and the bot's `max_access_token_s` ──


def _day_long(bots: dict[str, Any], name: str) -> None:
    """Let bot `name` issue tokens of up to a day, from workload tokens of a day."""
    bots["registered"][name] |= {"max_access_token_s": 86400, "max_token_s": 86400}


@pytest.mark.parametrize(
    ("maximum", "asked", "lives"),
    [
        (3600, None, 3600),  # nothing asked: an hour
        (86400, "", 3600),  # an empty value asks nothing
        (3600, "86400", 3600),  # more than the bot allows: its maximum, not a refusal
        (86400, "86400", 86400),
        (86400, "7200", 7200),
        (86400, None, 3600),  # a day allowed is not a day given unasked
        (600, None, 600),  # a maximum under an hour shortens the default too
        (86400, "60", 60),
        (86400, "99999999999999999999", 86400),
        (86400, "9" * 5000, 86400),  # never converted whole: Python refuses that many digits
        (86400, "0" * 40 + "7200", 7200),
    ],
)
def test_a_bots_token_lives_what_it_asks_up_to_its_maximum(
    bots, monkeypatch, maximum: int, asked: str | None, lives: int
) -> None:
    audited: list[dict[str, Any]] = []

    async def emit(event_type: str, **kw: Any) -> str:
        audited.append(kw["subject"])
        return ""

    monkeypatch.setattr(oauth.audit, "emit", emit)
    bots["registered"]["triage"]["max_access_token_s"] = maximum
    r = _with_key(TestClient(oauth_app()), _assertion(bots["key"]), expires_in=asked)
    assert r.status_code == 200, r.text
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == lives
    assert lives - 2 <= r.json()["expires_in"] <= lives
    assert [a["ttl_s"] for a in audited] == [lives]


@pytest.mark.parametrize(
    "asked", ["0", "59", "-60", "1.5", "1e4", "an hour", " 3600", "\u0663\u0666"]
)
def test_a_lifetime_loom_does_not_issue_is_refused_before_anything_is_spent(
    bots, asked: str
) -> None:
    bots["registered"]["triage"]["max_access_token_s"] = 86400
    c, assertion = TestClient(oauth_app()), _assertion(bots["key"])
    r = _with_key(c, assertion, expires_in=asked)
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    assert "expires_in" in r.json()["error_description"]
    assert _with_key(c, assertion).status_code == 200  # its assertion is still unspent
    refused = _exchange(c, _ci_token(bots), expires_in=asked)
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_request"
    assert bots["attempts"].rows == []  # an exchange refused so is never counted


def test_a_ci_exchange_asking_for_a_day_never_outlives_its_workload_token(bots) -> None:
    _day_long(bots, "deploy")
    c = TestClient(oauth_app())
    short = _exchange(c, _ci_token(bots), expires_in="86400")  # the job's token lives 5 minutes
    assert short.status_code == 200 and 240 <= short.json()["expires_in"] <= 300
    now = int(time.time())
    day = _ci_token(bots, iat=now, exp=now + 86400)
    long = _exchange(c, day, expires_in="86400")
    assert long.status_code == 200, long.text
    claims = pyjwt.decode(long.json()["access_token"], options={"verify_signature": False})
    assert claims["exp"] == now + 86400 and long.json()["expires_in"] >= 86400 - 5
    assert 3595 <= _exchange(c, day).json()["expires_in"] <= 3600  # unasked: an hour
    bots["registered"]["deploy"]["max_access_token_s"] = 7200
    capped = _exchange(c, day, expires_in="86400").json()["expires_in"]
    assert 7195 <= capped <= 7200


def test_a_day_long_agent_token_is_cut_by_every_revocation_of_it(bots, monkeypatch) -> None:
    """Subject and parent revocations refuse tokens whose expiry is a day away."""
    from datetime import UTC, datetime

    from server.auth.jwt import TokenError, verify_token

    _day_long(bots, "fleet")
    now = int(time.time())
    proof = _ci_token(bots, agent_id="a1", iat=now, exp=now + 86400)
    r = _exchange(TestClient(oauth_app()), proof, client_id="fleet", expires_in="86400")
    token = r.json()["access_token"]
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - time.time() > 86000  # its expiry is a day away
    cutoff = datetime.fromtimestamp(claims["iat"] + 1, UTC)
    rows = [
        {"jti": "sa:fleet:a1", "issued_before": None},  # for good
        {"jti": revocation.CUTOFF_PREFIX + "sa:fleet:a1", "issued_before": cutoff},
        {"jti": revocation.CUTOFF_PREFIX + "sa:fleet", "issued_before": cutoff},  # the trust's
    ]
    for row in rows:
        monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
        monkeypatch.setattr(revocation, "_revoked_before", {})
        monkeypatch.setattr(revocation, "_cut_off", {})
        monkeypatch.setattr(revocation, "_recent", [])
        assert verify_token(token).subject == "sa:fleet:a1"
        revocation.remember_row({**row, "version": 1})  # as each writer folds its row in
        with pytest.raises(TokenError, match="revoked"):
            verify_token(token)


async def test_a_subjects_revocation_outlasts_a_day_long_token(monkeypatch) -> None:
    """A subject's revocation survives the longest token it can end."""
    from datetime import UTC, datetime, timedelta

    rows: list[dict[str, Any]] = []

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"v": len(rows) + 1}  # next_version's read

        async def insert(self, table: str, batch: list[dict[str, Any]]) -> None:
            rows.extend(batch)

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_recent", [])
    await revocation.revoke("sa:fleet:a1", reason="left", revoked_by="ops")
    day_on = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    assert rows[0]["expires_at"] > day_on and revocation.never_lapses(rows[0])


def test_logging_out_a_day_long_token_keeps_its_revocation_until_it_expires(
    bots, monkeypatch
) -> None:
    from datetime import UTC, datetime

    bots["registered"]["triage"]["max_access_token_s"] = 86400
    c = TestClient(oauth_app())
    token = _with_key(c, _assertion(bots["key"]), expires_in="86400").json()["access_token"]
    written: list[dict[str, Any]] = []

    async def revoke(key: str, **kw: Any) -> dict[str, Any]:
        written.append({"jti": key, **kw})
        return written[-1]

    monkeypatch.setattr(revocation, "revoke", revoke)
    assert c.post("/oauth/revoke", data={"token": token, "client_id": "triage"}).status_code == 200
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert [w["jti"] for w in written] == [claims["jti"]]
    assert written[0]["expires_at"] == datetime.fromtimestamp(claims["exp"], UTC)
    assert claims["exp"] - claims["iat"] == 86400


# ── a platform-admin bot ──────────────────────────────────────────────────


def _platform_admin_trust(bots: dict[str, Any]) -> None:
    """A control plane's own trust: one identity whose tokens hold platform_admin."""
    bots["registered"]["cp"] = {
        **bots["registered"]["deploy"],
        "name": "cp",
        "ns_roles": [],
        "claims": {"sub": "controlplane"},
        "platform_admin": True,
    }


@pytest.mark.parametrize(
    ("asked", "proof_lives", "lives"),
    [(None, 300, 300), ("86400", 86400, 86400), ("86400", 300, 300)],
)
def test_an_exchange_carries_platform_admin_only_for_a_trust_registered_so(
    bots, asked: str | None, proof_lives: int, lives: int
) -> None:
    from server.auth.jwt import verify_token
    from server.operations.base import Role

    _platform_admin_trust(bots)
    _day_long(bots, "cp")
    now = int(time.time())
    c = TestClient(oauth_app())
    r = _exchange(
        c,
        _ci_token(bots, sub="controlplane", iat=now, exp=now + proof_lives),
        client_id="cp",
        expires_in=asked,
    )
    assert r.status_code == 200, r.text
    assert lives - 5 <= r.json()["expires_in"] <= lives
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["exp"] == now + lives
    assert claims["sub"] == "sa:cp" and claims["roles"] == ["platform_admin"]
    assert claims["namespaces"] == ["*"] and "ns_roles" not in claims
    info = verify_token(r.json()["access_token"])
    assert info.has_role(Role.PLATFORM_ADMIN) and not info.has_ns_roles()  # what admin gates ask
    # The same issuer's other trust gets its namespace roles, never the role.
    other = _exchange(c, _ci_token(bots), client_id="deploy").json()["access_token"]
    plain = pyjwt.decode(other, options={"verify_signature": False})
    assert "platform_admin" not in plain.get("roles", []) and plain["ns_roles"]
    assert not verify_token(other).has_role(Role.PLATFORM_ADMIN)


@pytest.mark.parametrize(
    ("maximum", "asked", "lives"),
    [(3600, None, 3600), (86400, "86400", 86400), (600, None, 600)],
)
def test_a_key_bot_registered_platform_admin_gets_the_role_too(
    bots, maximum: int, asked: str | None, lives: int
) -> None:
    bots["registered"]["triage"] |= {
        "ns_roles": [],
        "platform_admin": True,
        "max_access_token_s": maximum,
    }
    r = _with_key(TestClient(oauth_app()), _assertion(bots["key"]), expires_in=asked)
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert r.status_code == 200 and claims["roles"] == ["platform_admin"]
    assert claims["exp"] - claims["iat"] == lives
    assert lives - 2 <= r.json()["expires_in"] <= lives


def test_a_revoked_platform_admin_bot_gets_nothing(bots, monkeypatch) -> None:
    """Disabling the bot cuts off `sa:cp`: its exchange is refused from then on."""
    _platform_admin_trust(bots)

    async def revoked(subjects: list[str], *, issued_at: int) -> bool:
        return "sa:cp" in subjects

    monkeypatch.setattr(revocation, "subjects_revoked", revoked)
    r = _exchange(TestClient(oauth_app()), _ci_token(bots, sub="controlplane"), client_id="cp")
    assert r.status_code == 401 and "revoked" in r.text


def test_a_bot_without_the_flag_is_one_without_the_role(bots, monkeypatch) -> None:
    """A row or dict from a build before the flag carries no `platform_admin`:
    it saves as 0 and its exchange is an ordinary bot's, never a KeyError."""
    del bots["registered"]["deploy"]["platform_admin"]
    r = _exchange(TestClient(oauth_app()), _ci_token(bots))
    claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert r.status_code == 200 and claims["ns_roles"] and claims["roles"] == []
    saved: list[dict[str, Any]] = []

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"v": 1}

        async def insert(self, table: str, batch: list[dict[str, Any]]) -> None:
            saved.extend(batch)

    monkeypatch.setattr(machines, "get_client", lambda: _CH())
    asyncio.run(machines.save(bots["registered"]["deploy"], by="op"))
    assert saved[0]["platform_admin"] == 0
    assert machines._machine({**saved[0], "platform_admin": None})["platform_admin"] is False
