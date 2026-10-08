"""Unit coverage for SDK credential resolution from the environment.

Server-free: only inspects the Authorization header the Transport derives
at construction time. `LOOM_API_KEY` is the one credential variable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from loom import Agent


def _auth_header(agent: Agent) -> str | None:
    # The credential lives only in the httpx client's headers.
    return agent.transport._client.headers.get("Authorization")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    for var in ("LOOM_API_KEY", "LOOM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.loom/token to fall back to
    return monkeypatch


def _close(agent: Agent) -> None:
    asyncio.run(agent.close())


def test_loom_api_key_is_read(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LOOM_API_KEY", "tok-from-env")
    agent = Agent(org="dev", namespace="ns", agent="bot")
    try:
        assert _auth_header(agent) == "Bearer tok-from-env"
    finally:
        _close(agent)


def test_loom_token_is_not_read(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LOOM_TOKEN", "tok-old-name")
    agent = Agent(org="dev", namespace="ns", agent="bot")
    try:
        assert _auth_header(agent) is None
    finally:
        _close(agent)


def test_explicit_api_key_kwarg_wins(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LOOM_API_KEY", "tok-canonical")
    agent = Agent(org="dev", namespace="ns", agent="bot", api_key="tok-kwarg")
    try:
        assert _auth_header(agent) == "Bearer tok-kwarg"
    finally:
        _close(agent)


def test_no_credential_means_no_auth_header(clean_env: pytest.MonkeyPatch) -> None:
    agent = Agent(org="dev", namespace="ns", agent="bot")
    try:
        assert _auth_header(agent) is None
    finally:
        _close(agent)
