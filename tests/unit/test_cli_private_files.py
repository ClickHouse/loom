"""Files that hold a credential are written owner-only and atomically:
the token file, `.env` (set-host), client MCP configs."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

import pytest

from server.cli import shell
from server.cli._paths import write_private
from server.cli.init import write_config, write_token


def _translate_unset_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    """An open that leaves newline unset translates each ``\\n`` to CR LF."""
    real = os.fdopen

    def fdopen(fd, mode="r", buffering=-1, encoding=None, errors=None, newline=None, **kwargs):
        if "b" not in mode and newline is None:
            newline = "\r\n"
        return real(
            fd, mode, buffering, encoding=encoding, errors=errors, newline=newline, **kwargs
        )

    monkeypatch.setattr(os, "fdopen", fdopen)


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


def test_write_private_keeps_lf_and_crlf_when_newline_translates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _translate_unset_newline(monkeypatch)
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "nested" / "crlf.json"
    write_private(lf, "{}\n")
    write_private(crlf, "{\r\n}\r\n")
    assert lf.read_bytes() == b"{}\n"
    assert crlf.read_bytes() == b"{\r\n}\r\n"
    assert _mode(lf) == 0o600
    assert _mode(crlf) == 0o600 and _mode(crlf.parent) == 0o700
    assert [p.name for p in tmp_path.iterdir() if not p.is_dir()] == ["lf.json"]


def test_token_and_config_keep_lf_when_newline_translates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _translate_unset_newline(monkeypatch)
    token = tmp_path / "token"
    write_token("secret-token", token)
    assert token.read_bytes() == b"secret-token\n" and _mode(token) == 0o600
    cfg = tmp_path / "config.json"
    write_config(cfg, url="https://h", namespace="ns", org="org")
    assert b"\r" not in cfg.read_bytes() and cfg.read_bytes().endswith(b"\n")
    assert _mode(cfg) == 0o600


def test_set_host_keeps_lf_when_newline_translates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _translate_unset_newline(monkeypatch)
    env = tmp_path / ".env"
    env.write_bytes(b"LOOM_JWT_SECRET=s3cret\n")
    ns = argparse.Namespace(
        emails=["ops@example.com"], env_file=str(env), append=False, dry_run=False
    )
    assert shell._cmd_set_host(ns) == 0
    raw = env.read_bytes()
    assert b"\r" not in raw
    assert b"LOOM_JWT_SECRET=s3cret\n" in raw
    assert _mode(env) == 0o600
