"""A LOOM_* key no setting reads is named once at boot, never silently dropped."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from server.settings import Settings, log_unknown_env_keys
from server.settings._env import NON_SETTINGS_ENV, unknown_env_keys


def test_unknown_keys_come_from_the_environment_and_the_dotenv_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for k in [k for k in os.environ if k.startswith("LOOM_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("LOOM_MEMORY_PRIOR_CAP", "0.1")  # a field
    monkeypatch.setenv("LOOM_BASE_URL", "http://x")  # a known non-settings variable
    monkeypatch.setenv("LOOM_MEMROY_ASK_TOP_K", "5")  # a typo
    env = tmp_path / ".env"
    env.write_text("LOOM_RETIRED_FLAG=secret-value\nOPENAI_API_KEY=k\nLOOM_CH_HOST=h\n")
    assert unknown_env_keys(Settings.model_fields, str(env)) == [
        "LOOM_MEMROY_ASK_TOP_K",
        "LOOM_RETIRED_FLAG",
    ]
    assert unknown_env_keys(Settings.model_fields, None) == ["LOOM_MEMROY_ASK_TOP_K"]
    assert unknown_env_keys(Settings.model_fields, str(tmp_path / "missing")) == [
        "LOOM_MEMROY_ASK_TOP_K"
    ]


def test_the_warning_names_keys_never_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LOOM_ENV_FILE", "")
    monkeypatch.setenv("LOOM_NOT_A_SETTING", "hunter2")
    with caplog.at_level(logging.WARNING, logger="server.settings"):
        log_unknown_env_keys()
    assert "LOOM_NOT_A_SETTING" in caplog.text
    assert "hunter2" not in caplog.text


def test_the_settings_file_named_in_the_dotenv_file_is_called_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = tmp_path / ".env"
    env.write_text("LOOM_SETTINGS_FILE=/etc/loom.yaml\n")
    monkeypatch.setenv("LOOM_ENV_FILE", str(env))
    monkeypatch.delenv("LOOM_SETTINGS_FILE", raising=False)
    with caplog.at_level(logging.WARNING, logger="server.settings"):
        log_unknown_env_keys()
    assert "LOOM_SETTINGS_FILE in" in caplog.text and "is ignored" in caplog.text


def test_no_known_variable_is_also_a_field() -> None:
    fields = {f"LOOM_{f.upper()}" for f in Settings.model_fields}
    assert not NON_SETTINGS_ENV & fields
