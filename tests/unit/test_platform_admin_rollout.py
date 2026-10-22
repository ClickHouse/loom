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

    monkeypatch.setattr(instances, "live_rows", lambda: fleet)
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

    async def read_live(ch: Any) -> list[dict[str, Any]]:
        reads.append(ch)
        return fleet

    def mint(**kw: Any) -> str:
        minted.append(kw)
        return "token"

    monkeypatch.setattr(instances, "live_rows", lambda: [{"schema_version": 76}])
    monkeypatch.setattr(instances, "read_live", read_live)
    monkeypatch.setattr(keyring, "load_for_offline_mint", lambda **kw: None)
    monkeypatch.setattr(access, "run_connected", lambda run: asyncio.run(run(None)))
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

    def unread(run):
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
