"""Integration tests use the same explicitly selected dotenv as the server."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import conftest


def test_explicit_integration_env_wins_over_repo_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selected = tmp_path / "isolated.env"
    monkeypatch.setenv("LOOM_ENV_FILE", str(selected))
    assert conftest._integration_env_path() == selected


def test_empty_integration_env_disables_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_ENV_FILE", "")
    assert conftest._integration_env_path() is None
