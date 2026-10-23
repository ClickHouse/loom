"""A bot end to end against a real server: a platform admin registers it over
the API with a generated key, the SDK with only LOOM_CLIENT_ID and the key
file reads and writes as `sa:<name>` on hour-long tokens, and disabling it
ends both its current token and any new one. A bot allowed day-long tokens
gets one when its request asks, and revoking its subject ends it at once."""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

import jwt as pyjwt  # noqa: E402
from loom import Agent  # noqa: E402
from loom.errors import AuthInvalid  # noqa: E402
from loom.machine import MachineLogin  # noqa: E402

from server.auth.jwt import mint_token  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402
from tests.integration._spawn import loom_server  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    with loom_server("bots", {"LOOM_AUTH_REQUIRED": "true"}) as url:
        yield url


def _admin(base: str, path: str, body: dict, *, roles: list[str]) -> httpx.Response:
    # Signed with the secret this process and the server both read.
    token = mint_token(
        subject="ops@example.com",
        org="ops",
        namespaces=["*"],
        roles=roles,
        is_service_account=False,
    )
    # A replace or disable waits out its revocation cutoff before it answers.
    return httpx.post(
        f"{base}{path}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
    )


def _namespace(ch_client: Any, org: str, ns: str) -> None:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    ch_client.insert(
        "loom.namespaces",
        [[org, ns, "ops@example.com", "bots e2e", True, now, now]],
        column_names=[
            "org",
            "namespace",
            "owner_email",
            "description",
            "active",
            "created_at",
            "updated_at",
        ],
    )


def test_a_bot_signs_in_with_its_key_and_is_cut_off_when_disabled(
    base: str, ch_client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns, name = unique_org("bots"), f"bot-{uuid.uuid4().hex[:6]}", f"b{uuid.uuid4().hex[:6]}"
    _namespace(ch_client, org, ns)  # a bot is given namespaces that exist
    body = {"name": name, "org": org, "namespace_roles": [f"{ns}=writer"], "generate_key": True}
    assert _admin(base, "/v1/admin/bots.create", body, roles=["writer"]).status_code == 403
    r = _admin(base, "/v1/admin/bots.create", body, roles=["platform_admin"])
    assert r.status_code == 200, r.text
    key = tmp_path / "bot.pem"
    key.write_text(r.json()["private_key_pem"])
    monkeypatch.setenv("LOOM_CLIENT_ID", name)
    monkeypatch.setenv("LOOM_CLIENT_KEY_FILE", str(key))
    monkeypatch.delenv("LOOM_API_KEY", raising=False)

    async def write_and_read() -> str:
        async with Agent(org=org, namespace=ns, agent="itest", base_url=base) as agent:
            await agent.memory.set("bot/fact/1", "written by a bot")
            got = await agent.memory.get("bot/fact/1")
            return str(got.value if hasattr(got, "value") else got)

    assert "written by a bot" in asyncio.run(write_and_read())
    login = MachineLogin(base, name, key_file=str(key))
    held = login.token()
    whoami = httpx.get(f"{base}/v1/whoami", headers={"Authorization": f"Bearer {held}"}).json()
    assert whoami["subject"] == f"sa:{name}"

    r = _admin(base, "/v1/admin/bots.disable", {"name": name}, roles=["platform_admin"])
    assert r.json() == {"disabled": True}
    # whoami answers a refused token with authenticated: false.
    gone = httpx.get(f"{base}/v1/whoami", headers={"Authorization": f"Bearer {held}"}).json()
    assert gone["authenticated"] is False and "revoked" in gone["reason"]
    with pytest.raises(AuthInvalid):
        MachineLogin(base, name, key_file=str(key)).token()


def test_a_bot_allowed_a_day_gets_one_when_asked_and_its_revocation_ends_it(
    base: str, ch_client: Any, tmp_path: Path
) -> None:
    org, ns, name = unique_org("bots"), f"bot-{uuid.uuid4().hex[:6]}", f"b{uuid.uuid4().hex[:6]}"
    _namespace(ch_client, org, ns)
    body = {
        "name": name,
        "org": org,
        "namespace_roles": [f"{ns}=writer"],
        "generate_key": True,
        "max_access_token_s": 86400,
    }
    r = _admin(base, "/v1/admin/bots.create", body, roles=["platform_admin"])
    assert r.status_code == 200, r.text
    key = tmp_path / "bot.pem"
    key.write_text(r.json()["private_key_pem"])
    listing = _admin(base, "/v1/admin/bots.list", {}, roles=["platform_admin"]).json()["bots"]
    assert [b["max_access_token_s"] for b in listing if b["name"] == name] == [86400]
    meta = httpx.get(f"{base}/.well-known/oauth-authorization-server", timeout=30.0).json()

    def issued(expires_in: str | None) -> dict[str, Any]:
        # The SDK never asks for a lifetime: the form is built here, with its assertion.
        now = int(time.time())
        claims = {"iss": name, "sub": name, "aud": meta["token_endpoint"], "iat": now}
        claims |= {"exp": now + 120, "jti": uuid.uuid4().hex}
        assertion = pyjwt.encode(claims, key.read_bytes(), algorithm="ES256")
        form = {
            "grant_type": "client_credentials",
            "client_id": name,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            **({} if expires_in is None else {"expires_in": expires_in}),
        }
        answer = httpx.post(f"{base}/oauth/token", data=form, timeout=30.0)
        assert answer.status_code == 200, answer.text
        return answer.json()

    assert 3590 <= issued(None)["expires_in"] <= 3600  # unasked: an hour
    assert 86390 <= issued("999999")["expires_in"] <= 86400  # the bot's maximum
    day = issued("86400")["access_token"]
    claims = pyjwt.decode(day, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 86400
    headers = {"Authorization": f"Bearer {day}"}
    assert httpx.get(f"{base}/v1/whoami", headers=headers).json()["subject"] == f"sa:{name}"

    revoked = _admin(
        base,
        "/v1/admin/revoke_subject",
        {"org": org, "subject": f"sa:{name}", "reason": "bots e2e"},
        roles=["platform_admin"],
    )
    assert revoked.status_code == 200, revoked.text
    gone = httpx.get(f"{base}/v1/whoami", headers=headers).json()
    assert gone["authenticated"] is False and "revoked" in gone["reason"]


def test_a_platform_admin_bot_exchanges_its_key_without_namespace_grants(
    base: str, ch_client: Any, tmp_path: Path
) -> None:
    org, name = unique_org("bots"), f"admin{uuid.uuid4().hex[:6]}"
    body = {
        "name": name,
        "org": org,
        "namespace_roles": [],
        "generate_key": True,
        "platform_admin": True,
    }
    registered = _admin(base, "/v1/admin/bots.create", body, roles=["platform_admin"])
    assert registered.status_code == 200, registered.text
    [saved] = ch_client.query(
        "SELECT name, org, ns_roles, platform_admin, active FROM loom.machine_identities FINAL "
        "WHERE name = {name:String}",
        parameters={"name": name},
    ).result_rows
    assert saved == (name, org, [], 1, 1)
    key = tmp_path / "admin.pem"
    key.write_text(registered.json()["private_key_pem"])
    token = MachineLogin(base, name, key_file=str(key)).token()
    headers = {"Authorization": f"Bearer {token}"}
    whoami = httpx.get(f"{base}/v1/whoami", headers=headers).json()
    assert whoami["authenticated"] is True
    assert whoami["subject"] == f"sa:{name}"
    listed = httpx.post(f"{base}/v1/admin/bots.list", headers=headers)
    assert listed.status_code == 200, listed.text
    [bot] = [row for row in listed.json()["bots"] if row["name"] == name]
    assert bot["platform_admin"] is True and bot["namespace_roles"] == []
    successor = httpx.post(
        f"{base}/v1/admin/bots.create",
        json={**body, "name": f"next{uuid.uuid4().hex[:6]}"},
        headers=headers,
    )
    assert successor.status_code == 403, successor.text
