"""The CLI loads the same dotenv file Settings reads: LOOM_ENV_FILE, else
`.env` in the working directory, never one found by searching upward from
the installed module; the shell's exports win."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.cli import app


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    # The loader writes os.environ itself. setenv first, so monkeypatch records
    # each name's state before the test and restores it after, whatever the
    # loader added (delenv of an absent name records nothing).
    for k in ("LOOM_ENV_FILE", "ZZ_DOTENV_A", "ZZ_DOTENV_B"):
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    return tmp_path


def test_reads_the_working_directory_env_without_overriding_exports(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    (cwd / ".env").write_text("ZZ_DOTENV_A=file\nZZ_DOTENV_B=file\n")
    monkeypatch.setenv("ZZ_DOTENV_B", "shell")
    app._load_dotenv_like_the_container()
    assert os.environ["ZZ_DOTENV_A"] == "file" and os.environ["ZZ_DOTENV_B"] == "shell"


def test_loom_env_file_names_the_file_and_empty_reads_none(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    (cwd / ".env").write_text("ZZ_DOTENV_A=cwd\n")
    (cwd / "other.env").write_text("ZZ_DOTENV_A=other\n")
    monkeypatch.setenv("LOOM_ENV_FILE", "")
    app._load_dotenv_like_the_container()
    assert "ZZ_DOTENV_A" not in os.environ
    monkeypatch.setenv("LOOM_ENV_FILE", str(cwd / "other.env"))
    app._load_dotenv_like_the_container()
    assert os.environ["ZZ_DOTENV_A"] == "other"


def test_no_env_in_the_working_directory_reads_nothing(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    (cwd / "sub").mkdir()
    (cwd / ".env").write_text("ZZ_DOTENV_A=parent\n")
    monkeypatch.chdir(cwd / "sub")
    app._load_dotenv_like_the_container()
    assert "ZZ_DOTENV_A" not in os.environ
