"""Machine admin tokens wait until every replica protects human credentials."""

from __future__ import annotations

import time
from typing import Any

import pytest

from server import cli
from server.auth import oauth
from server.core import instances

_FLEETS = [
    ([], False),
    ([{"schema_version": 73}], False),
    ([{"schema_version": 76}, {"schema_version": 75}], False),
    ([{"schema_version": None}], False),
    ([{"schema_version": 75}], False),
    ([{"schema_version": 76}], True),
    ([{"schema_version": 76}, {"schema_version": 77}], True),
]


@pytest.mark.parametrize(("fleet", "ready"), _FLEETS)
async def test_machine_admin_exchange_requires_an_upgraded_fleet(
    monkeypatch: pytest.MonkeyPatch, fleet: list[dict[str, Any]], ready: bool
) -> None:
    from server.auth import revocation

    minted: list[dict[str, Any]] = []

    async def not_revoked(*a: Any, **kw: Any) -> bool:
        return False

    async def recorded(*a: Any, **kw: Any) -> str:
        return "hash"

    def mint(**kw: Any) -> str:
        minted.append(kw)
        return "token"

    async def read_live(ch, *, all_deployments):
        assert all_deployments
        return fleet

    monkeypatch.setattr(instances, "read_live", read_live)
    monkeypatch.setattr(revocation, "subjects_revoked", not_revoked)
    monkeypatch.setattr(oauth.audit, "emit", recorded)
    monkeypatch.setattr(oauth, "mint_token", mint)
    bot = {"name": "controlplane", "org": "acme", "platform_admin": True}
    now = int(time.time())
    if ready:
        answer = await oauth._machine_tokens(bot, now, now + 3600, {"by": "key"})
        assert answer["access_token"] == "token" and minted[0]["roles"] == ["platform_admin"]
    else:
        with pytest.raises(oauth.OAuthError) as refused:
            await oauth._machine_tokens(bot, now, now + 3600, {"by": "key"})
        assert refused.value.status_code == 503
        assert refused.value.error == "temporarily_unavailable"
        assert minted == []


@pytest.mark.parametrize(("fleet", "ready"), _FLEETS)
def test_cli_admin_mint_reads_the_fleet_before_minting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fleet: list[dict[str, Any]],
    ready: bool,
) -> None:
    import asyncio

    from server.auth import jwt, keyring
    from server.cli import access

    reads: list[Any] = []
    minted: list[dict[str, Any]] = []

    async def read_live(ch: Any, *, all_deployments: bool) -> list[dict[str, Any]]:
        assert all_deployments
        reads.append(ch)
        return fleet

    def mint(**kw: Any) -> str:
        minted.append(kw)
        return "token"

    monkeypatch.setattr(instances, "live_rows", lambda: [{"schema_version": 76}])
    monkeypatch.setattr(instances, "read_live", read_live)
    monkeypatch.setattr(keyring, "load_for_offline_mint", lambda **kw: None)
    monkeypatch.setattr(access, "run_connected", lambda run, **_declared: asyncio.run(run(None)))
    monkeypatch.setattr(access, "_audit_platform_admin_mint", lambda *a: True)
    monkeypatch.setattr(jwt, "mint_token", mint)
    code = cli.main(
        [
            "mint-token",
            "--subject",
            "sa:controlplane",
            "--org",
            "acme",
            "--platform-admin",
            "--ttl-days",
            "7",
            "--json",
        ]
    )
    out, err = capsys.readouterr()
    assert reads == [None]
    if ready:
        assert code == 0 and '"token": "token"' in out
        assert minted[0]["roles"] == ["platform_admin"]
    else:
        assert code != 0 and out == "" and "rollout" in err
        assert minted == []


def test_cli_admin_mint_refuses_an_unread_fleet(monkeypatch, capsys) -> None:
    from server.auth import jwt
    from server.cli import access

    def unread(run, **_declared: object):
        raise ConnectionError("offline")

    def mint(**kw):
        raise AssertionError("an unread fleet must not mint")

    monkeypatch.setattr(access, "run_connected", unread)
    monkeypatch.setattr(jwt, "mint_token", mint)
    code = cli.main(
        [
            "mint-token",
            "--subject",
            "sa:controlplane",
            "--org",
            "acme",
            "--platform-admin",
            "--ttl-days",
            "7",
        ]
    )
    out, err = capsys.readouterr()
    assert code == 1 and out == "" and "rollout" in err


def test_admin_token_floor_matches_the_physical_migration() -> None:
    from pathlib import Path

    from server.auth import machines

    path = Path(__file__).resolve().parents[2] / "schemas/migrations/076_machine_platform_admin.sql"
    assert int(path.name.split("_", 1)[0]) == machines._PLATFORM_ADMIN_MIGRATION
    assert path.is_file()


class Fleet:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows
            if params.get("all_deployments") or row["deployment"] == params["dep"]
        ]


@pytest.mark.parametrize("deployment", [False, True])
async def test_exchange_sees_older_replicas_of_the_other_kind(monkeypatch, deployment) -> None:
    from server.auth import revocation
    from server.core import ch
    from server.settings import settings

    fleet = Fleet(
        [
            {"deployment": int(deployment), "schema_version": 76},
            {"deployment": int(not deployment), "schema_version": 73},
        ]
    )

    async def not_revoked(*a, **kw):
        return False

    def mint(**kw):
        raise AssertionError("an older replica of either kind must refuse issuance")

    monkeypatch.setattr(settings, "deployment", deployment)
    monkeypatch.setattr(ch, "get_client", lambda: fleet)
    monkeypatch.setattr(instances, "live_rows", lambda: [{"schema_version": 76}])
    monkeypatch.setattr(revocation, "subjects_revoked", not_revoked)
    monkeypatch.setattr(oauth, "mint_token", mint)
    bot = {"name": "controlplane", "org": "acme", "platform_admin": True}
    now = int(time.time())
    with pytest.raises(oauth.OAuthError) as refused:
        await oauth._machine_tokens(bot, now, now + 3600, {"by": "key"})
    assert refused.value.status_code == 503


@pytest.mark.parametrize("deployment", [False, True])
def test_cli_sees_older_replicas_of_the_other_kind(monkeypatch, capsys, deployment) -> None:
    import asyncio

    from server.auth import jwt
    from server.cli import access
    from server.settings import settings

    fleet = Fleet(
        [
            {"deployment": int(deployment), "schema_version": 76},
            {"deployment": int(not deployment), "schema_version": 73},
        ]
    )

    def mint(**kw):
        raise AssertionError("an older replica of either kind must refuse issuance")

    monkeypatch.setattr(settings, "deployment", deployment)
    monkeypatch.setattr(access, "run_connected", lambda run, **_declared: asyncio.run(run(fleet)))
    monkeypatch.setattr(jwt, "mint_token", mint)
    code = cli.main(
        [
            "mint-token",
            "--subject",
            "sa:controlplane",
            "--org",
            "acme",
            "--platform-admin",
            "--ttl-days",
            "7",
            "--json",
        ]
    )
    out, err = capsys.readouterr()
    assert code == 1 and out == "" and "rollout" in err


@pytest.mark.parametrize("deployment", [False, True])
async def test_admin_fleet_read_preserves_the_normal_replica_cache(monkeypatch, deployment) -> None:
    from server.auth import machines
    from server.settings import settings

    current = {"deployment": int(deployment), "schema_version": 76}
    old = {"deployment": int(not deployment), "schema_version": 73}
    fleet = Fleet([current, old])
    monkeypatch.setattr(settings, "deployment", deployment)
    monkeypatch.setattr(instances, "_live", None)
    assert await instances.read_live(fleet) == [current]
    assert instances.live_rows() == [current]
    assert not await machines.platform_admin_tokens_ready(fleet)
    assert instances.live_rows() == [current]


async def test_exchange_refuses_an_unread_fleet(monkeypatch) -> None:
    from server.auth import revocation

    async def unread(*a, **kw):
        raise ConnectionError("offline")

    async def not_revoked(*a, **kw):
        return False

    def mint(**kw):
        raise AssertionError("an unread fleet must not mint")

    monkeypatch.setattr(instances, "read_live", unread)
    monkeypatch.setattr(revocation, "subjects_revoked", not_revoked)
    monkeypatch.setattr(oauth, "mint_token", mint)
    bot = {"name": "controlplane", "org": "acme", "platform_admin": True}
    now = int(time.time())
    with pytest.raises(oauth.OAuthError) as refused:
        await oauth._machine_tokens(bot, now, now + 3600, {"by": "key"})
    assert refused.value.status_code == 503
