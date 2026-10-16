"""A bot end to end against a real server: a platform admin registers it over
the API with a generated key, the SDK with only LOOM_CLIENT_ID and the key
file reads and writes as `sa:<name>` on hour-long tokens, and disabling it
ends both its current token and any new one."""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

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
    token = mint_token(subject="ops@example.com", org="ops", namespaces=["*"], roles=roles)
    return httpx.post(f"{base}{path}", json=body, headers={"Authorization": f"Bearer {token}"})


def test_a_bot_signs_in_with_its_key_and_is_cut_off_when_disabled(
    base: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns, name = unique_org("bots"), f"bot-{uuid.uuid4().hex[:6]}", f"b-{uuid.uuid4().hex[:6]}"
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
