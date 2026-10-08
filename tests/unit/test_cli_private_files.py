"""Files that hold a credential are written owner-only and atomically:
the token file, `.env` (set-host), client MCP configs."""

from __future__ import annotations

import argparse
import stat
from pathlib import Path

import pytest

from server.cli import shell
from server.cli._paths import write_private
from server.cli.init import write_token


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_replacing_a_loose_file_is_0600_from_the_first_byte(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("old\n")
    token.chmod(0o644)
    write_token(" new-token ", token)
    assert token.read_text() == "new-token\n" and _mode(token) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["token"]  # no temp left behind


def test_a_missing_parent_is_created_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "new" / "dir" / "config.json"
    write_private(path, "{}\n")
    assert _mode(path) == 0o600 and _mode(path.parent) == 0o700


def test_a_failed_write_keeps_the_old_file_and_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("kept\n")

    with pytest.raises(TypeError):
        write_private(path, object())  # type: ignore[arg-type]
    assert path.read_text() == "kept\n" and [p.name for p in tmp_path.iterdir()] == ["token"]


def test_set_host_rewrites_env_owner_only(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LOOM_JWT_SECRET=s3cret\n")
    env.chmod(0o644)
    ns = argparse.Namespace(
        emails=["Ops@Example.com"], env_file=str(env), append=False, dry_run=False
    )
    assert shell._cmd_set_host(ns) == 0
    text = env.read_text()
    assert "LOOM_JWT_SECRET=s3cret\n" in text and "LOOM_PLATFORM_ADMINS=ops@example.com" in text
    assert text.endswith("\n") and _mode(env) == 0o600
