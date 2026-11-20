"""`loom bot create --generate-key`: a refused bot leaves no key behind, and a
failure that may have come after the save keeps the key the bot stands on."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.auth.grant_claims import NotClaimed
from server.cli import bots
from server.core.errors import BackendUnavailableError, GrantBusyError
from server.identity.app import account_admin


def _args(key: Path) -> argparse.Namespace:
    return argparse.Namespace(
        name="triage",
        org="acme",
        namespace_role=["triage=writer"],
        generate_key=str(key),
        public_key=None,
        issuer="",
        audience="",
        claim=[],
        jwks_uri="",
        max_token_age=3600,
        max_agents=1000,
        max_access_token_ttl=3600,
        agent_claim="",
        platform_admin=False,
        replace=False,
    )


@pytest.fixture(autouse=True)
def _offline(monkeypatch) -> None:
    monkeypatch.setattr(bots, "run_connected", lambda run, **_declared: asyncio.run(run(None)))


@pytest.mark.parametrize(
    ("error", "kept"),
    [
        (ValueError("bot exists"), False),
        (GrantBusyError("a namespace is changing"), False),  # its claim not had: nothing saved
        (NotClaimed("a namespace is changing", operation="grant_change"), False),
        (BackendUnavailableError("timeout", operation="grant_change"), True),  # maybe saved
        (RuntimeError("CH away"), True),
    ],
)
def test_the_key_goes_only_with_a_refused_bot(monkeypatch, tmp_path, error, kept) -> None:
    async def create_bot(bot: Any, *, by: str, replace: bool) -> None:
        raise error

    monkeypatch.setattr(account_admin, "create_bot", create_bot)
    key = tmp_path / "bot.pem"
    if kept:
        with pytest.raises(type(error)):
            bots._cmd_create(_args(key))
    else:
        assert bots._cmd_create(_args(key)) == 2
    assert key.exists() is kept


def test_a_bots_longest_token_is_given_on_the_command_line(monkeypatch, tmp_path) -> None:
    saved: list[Any] = []

    async def create_bot(bot: Any, *, by: str, replace: bool) -> None:
        saved.append(bot)

    monkeypatch.setattr(account_admin, "create_bot", create_bot)
    args = _args(tmp_path / "bot.pem")
    args.max_access_token_ttl = 86400
    assert bots._cmd_create(args) == 0
    assert saved[0]["max_access_token_s"] == 86400
    args = _args(tmp_path / "other.pem")
    args.max_access_token_ttl = 86401
    assert bots._cmd_create(args) == 2 and len(saved) == 1


def test_bot_list_shows_a_shorter_maximum_apart_from_the_proofs(monkeypatch, capsys) -> None:
    from server.auth import machines

    row = {"has_key": 1, "issuer": "", "agent_claim": "", "active": 1, "org": "acme"}
    row |= {"ns_roles": [("triage", "writer")], "platform_admin": 0}

    async def listed() -> list[dict[str, Any]]:
        return [
            row | {"name": "hourly", "max_access_token_s": 3600},
            row | {"name": "brief", "max_access_token_s": 600},
        ]

    monkeypatch.setattr(machines, "listed", listed)
    assert bots._cmd_list(argparse.Namespace()) == 0
    hourly, brief = capsys.readouterr().out.splitlines()
    assert hourly.endswith(" key") and "max access token" not in hourly
    assert brief.endswith(" key  max access token 600 s")
