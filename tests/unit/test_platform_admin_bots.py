"""A machine identity may hold platform_admin (`loom bot create --platform-admin`,
`/v1/admin/bots.create`), and an operator may mint an expiring platform-admin
token for a service account (`loom mint-token --platform-admin`).

The role spans every org, so such a bot is one identity (no agent claim) with
no namespace roles, its registration is audited with the flag, the trust check
that keeps two trusts of one issuer from taking one token holds as before, and
a minted platform-admin token always expires.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import cli
from server.auth import machines, revocation
from server.identity.app import account_admin, admin_account_routes

_ISSUER = "https://cp.example/workload-identity"
_SECRET = "unit-test-secret-for-platform-admin-tokens"


def _trust(name: str, **kw: Any) -> machines.Machine:
    args: dict[str, Any] = {
        "name": name,
        "org": "acme",
        "namespace_roles": [],
        "issuer": _ISSUER,
        "audience": "loom",
    }
    return account_admin.bot_from(**(args | kw))


def test_a_platform_admin_bot_is_one_identity_with_no_namespace_role() -> None:
    bot = _trust("controlplane", claims=["sub=controlplane"], platform_admin=True)
    assert bot["platform_admin"] is True and bot["ns_roles"] == []
    keyed = account_admin.bot_from(
        name="ops",
        org="acme",
        namespace_roles=[],
        public_key_pem=machines.generate_key()[1].decode(),
        platform_admin=True,
    )
    assert keyed["platform_admin"] is True
    with pytest.raises(ValueError, match="takes no agent claim"):
        _trust("cp", claims=["tenant_id=t1"], agent_claim="agent_id", platform_admin=True)
    with pytest.raises(ValueError, match="no namespace role"):
        _trust("cp", claims=["sub=cp"], namespace_roles=["docs=viewer"], platform_admin=True)
    with pytest.raises(ValueError, match="at least one namespace role"):
        _trust("plain", claims=["sub=x"])  # without the role a bot still needs one
    assert (
        _trust("plain", claims=["sub=x"], namespace_roles=["d=viewer"])["platform_admin"] is False
    )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The bots saved, read back as `listed` reads them, and the audit rows."""
    state: dict[str, Any] = {"bots": {}, "audits": []}

    async def get(name: str, *, active_only: bool = True, ch: Any = None) -> Any:
        return state["bots"].get(name)

    async def save(bot: machines.Machine, *, by: str, active: bool = True, ch: Any = None) -> None:
        state["bots"][bot["name"]] = {**bot, "updated_by": by}

    async def listed() -> list[dict[str, Any]]:
        return [
            {**b, "active": 1, "has_key": bool(b["public_keys"])} for b in state["bots"].values()
        ]

    async def not_revoked(subject: str) -> bool:
        return False

    async def emit(event: str, **kw: Any) -> str:
        state["audits"].append((event, kw))
        return ""

    monkeypatch.setattr(machines, "get", get)
    monkeypatch.setattr(machines, "save", save)
    monkeypatch.setattr(machines, "listed", listed)
    monkeypatch.setattr(revocation, "revoked_outright", not_revoked)
    monkeypatch.setattr(account_admin.audit, "emit", emit)
    return state


def test_the_shared_token_check_takes_trusts_apart_on_sub(registry) -> None:
    """A control plane's platform-admin trust and its tenants' agent trusts can
    share one issuer when their `sub` claims cannot meet one token; with nothing
    to tell them apart, the agent trust is refused as before."""
    create = account_admin.create_bot
    asyncio.run(
        create(
            _trust("controlplane", claims=["sub=controlplane"], platform_admin=True),
            by="op",
            replace=False,
        )
    )
    tenant = _trust(
        "nerve-t1",
        claims=["tenant_id=t1", "sub=tenants/t1/agents/*"],
        agent_claim="agent_id",
    )
    asyncio.run(create(tenant, by="op", replace=False))
    assert set(registry["bots"]) == {"controlplane", "nerve-t1"}
    loose = _trust("nerve-t2", claims=["tenant_id=t2"], agent_claim="agent_id")
    with pytest.raises(ValueError, match="could also meet 'controlplane'"):
        asyncio.run(create(loose, by="op", replace=False))
    made = [kw["subject"] for event, kw in registry["audits"] if event == "auth.machine_created"]
    assert [(m["name"], m["platform_admin"]) for m in made] == [
        ("controlplane", True),
        ("nerve-t1", False),
    ]


def test_the_admin_api_registers_and_lists_the_flag(registry, monkeypatch) -> None:
    from server.insights.app import admin_routes

    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: False)
    app = FastAPI()
    app.include_router(admin_account_routes.router)
    c = TestClient(app)
    body = {
        "name": "controlplane",
        "org": "acme",
        "namespace_roles": [],
        "issuer": _ISSUER,
        "audience": "loom",
        "claims": ["sub=controlplane"],
        "platform_admin": True,
    }
    assert c.post("/v1/admin/bots.create", json=body).status_code == 200
    [listed] = c.post("/v1/admin/bots.list").json()["bots"]
    assert listed["name"] == "controlplane" and listed["platform_admin"] is True
    refused = c.post("/v1/admin/bots.create", json={**body, "name": "x", "agent_claim": "agent_id"})
    assert refused.status_code == 422 and "takes no agent claim" in refused.text


def test_loom_bot_create_takes_the_flag(registry, monkeypatch, capsys) -> None:
    from server.cli import bots as bots_cli

    monkeypatch.setattr(bots_cli, "run_connected", lambda run: asyncio.run(run(None)))
    rc = cli.main(
        [
            "bot",
            "create",
            "--name",
            "controlplane",
            "--org",
            "acme",
            "--issuer",
            _ISSUER,
            "--audience",
            "loom",
            "--claim",
            "sub=controlplane",
            "--platform-admin",
        ]
    )
    assert rc == 0 and registry["bots"]["controlplane"]["platform_admin"] is True
    assert "every org" in capsys.readouterr().out
    rc = cli.main(
        ["bot", "create", "--name", "x", "--org", "acme", "--issuer", _ISSUER, "--audience", "loom"]
        + ["--claim", "tenant_id=t1", "--agent-claim", "agent_id", "--platform-admin"]
    )
    assert rc == 2 and "takes no agent claim" in capsys.readouterr().err


@pytest.fixture
def minting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import access
    from server.settings import settings

    monkeypatch.setenv("LOOM_JWT_SECRET", _SECRET)
    monkeypatch.setattr(settings, "jwt_secret", _SECRET, raising=False)
    # An empty keyring: the mint signs with the root secret, reading no ClickHouse.
    monkeypatch.setattr(access, "run_connected", lambda run: None)


_MINT = ["mint-token", "--org", "acme", "--platform-admin"]


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        (["--subject", "sa:cp"], "--ttl-days"),  # never indefinite
        (["--subject", "sa:cp", "--ttl-days", "0"], "--ttl-days"),
        (["--subject", "lee", "--ttl-days", "7"], "sa:<name>"),  # a person signs in instead
        (["--subject", "sa:cp", "--ttl-days", "7", "--user-token"], "--user-token"),
        (["--subject", "sa:cp", "--ttl-days", "7", "--namespace-role", "a=admin"], "flat"),
    ],
)
def test_a_platform_admin_mint_must_expire_and_be_a_service_accounts(
    minting, capsys, extra: list[str], said: str
) -> None:
    assert cli.main([*_MINT, *extra]) == 2
    assert said in capsys.readouterr().err


def test_a_platform_admin_mint_holds_the_role_until_it_expires(minting, capsys) -> None:
    from server.auth.jwt import verify_token
    from server.operations.base import Role

    assert cli.main([*_MINT, "--subject", "sa:cp", "--ttl-days", "7", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["roles"] == ["platform_admin"] and summary["namespaces"] == ["*"]
    claims = pyjwt.decode(summary["token"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 7 * 86400
    info = verify_token(summary["token"], secret=_SECRET)
    assert info.has_role(Role.PLATFORM_ADMIN) and not info.has_ns_roles()
