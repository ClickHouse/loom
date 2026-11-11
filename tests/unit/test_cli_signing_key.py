"""`loom signing-key` against a scripted ClickHouse."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.cli import signing_keys
from server.cli.app import main
from server.core import leases


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "sealed_key" in sql:  # the keyring read: k1 signs
            from datetime import datetime

            from server.core.secrets_at_rest import seal

            return [
                {
                    "kid": "k1",
                    "algorithm": "HS256",
                    "sealed_key": seal("signing_key", "s" * 40),
                    "public_pem": "",
                    "activate_at": datetime(2026, 1, 1),
                    "retired": False,
                }
            ]
        return [{"kid": "k1", "algorithm": "HS256", "activate_at": "t", "retired_at": None}]

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        return None

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", "r" * 40)
    fake = _CH()
    monkeypatch.setattr(
        signing_keys, "run_connected", lambda run, **_declared: asyncio.run(run(fake))
    )

    class _Held:
        held = True

        @asynccontextmanager
        async def renewed(self) -> Any:
            yield self

    async def take(*_: Any, **__: Any) -> _Held:
        return _Held()

    monkeypatch.setattr(leases, "take", take)  # the retire claim
    return fake


def test_rotate_refuses_the_dev_secret_and_a_short_activation(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key sealed with the dev secret is one no server unseals; one that signs
    before every server has read it is refused by the others."""
    from server.settings import DEV_JWT_SECRET, settings

    assert main(["signing-key", "rotate", "--activate-in", "1"]) == 1
    monkeypatch.setattr(settings, "jwt_secret", DEV_JWT_SECRET)
    assert main(["signing-key", "rotate"]) == 1
    assert ch.inserts == []


def test_signing_key_rotate_list_and_retire(ch: _CH, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["signing-key", "rotate"]) == 0
    table, rows = ch.inserts[-1]
    assert table == "loom.signing_keys" and rows[0]["algorithm"] == "HS256"
    assert rows[0]["kid"] in capsys.readouterr().out
    assert ch.inserts[0][1][0]["kid"] == "default"  # the root secret, recorded
    assert main(["signing-key", "list"]) == 0
    out = capsys.readouterr().out
    assert "default  HS256  (LOOM_JWT_SECRET)" in out and "k1  HS256" in out
    assert main(["signing-key", "retire", "nope"]) == 1
    assert main(["signing-key", "retire", "default"]) == 0
    assert ch.inserts[-1][1][0]["kid"] == "default"


def test_signing_key_record_root_records_this_root_secret(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an on-purpose root replacement: the keyring commands accept the new one."""
    from server.auth import keyring
    from server.settings import DEV_JWT_SECRET, settings

    assert main(["signing-key", "record-root"]) == 0
    table, rows = ch.inserts[-1]
    assert table == "loom.signing_keys" and rows[0]["kid"] == "default"
    assert keyring._is_this_root(rows[0]["sealed_key"])
    monkeypatch.setattr(settings, "jwt_secret", DEV_JWT_SECRET)
    assert main(["signing-key", "record-root"]) == 1
    assert len(ch.inserts) == 1
