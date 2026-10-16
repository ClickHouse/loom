"""`loom bot create --generate-key`: a refused bot leaves no key behind, and a
failure that may have come after the save keeps the key the bot stands on."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.cli import bots
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
        agent_claim="",
        replace=False,
    )


@pytest.fixture(autouse=True)
def _offline(monkeypatch) -> None:
    monkeypatch.setattr(bots, "run_connected", lambda run: asyncio.run(run(None)))


@pytest.mark.parametrize(
    ("error", "kept"), [(ValueError("bot exists"), False), (RuntimeError("CH away"), True)]
)
def test_the_key_goes_only_with_a_refused_bot(monkeypatch, tmp_path, error, kept) -> None:
    async def create_bot(bot: Any, *, by: str, replace: bool) -> None:
        raise error

    monkeypatch.setattr(account_admin, "create_bot", create_bot)
    key = tmp_path / "bot.pem"
    if kept:
        with pytest.raises(RuntimeError):
            bots._cmd_create(_args(key))
    else:
        assert bots._cmd_create(_args(key)) == 2
    assert key.exists() is kept
