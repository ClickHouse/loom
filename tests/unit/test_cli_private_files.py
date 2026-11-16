"""Files that hold a credential are written owner-only and atomically:
the token file, `.env` (set-host), client MCP configs."""

from __future__ import annotations

import argparse
import stat
from pathlib import Path

import pytest

from server.cli import shell
from server.cli._paths import write_private
from server.cli.init import write_config, write_token


def _text_stream(path: Path, text: str, newline: str | None) -> bytes:
    with path.open("w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return path.read_bytes()


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


def test_a_crlf_text_stream_adds_a_cr_on_a_real_file(tmp_path: Path) -> None:
    text = '{\r\n  "theme": "One Dark",\r\n}\r\n'
    raw = _text_stream(tmp_path / "stream.json", text, "\r\n")
    assert raw != text.encode()
    assert raw.count(b"\r") - raw.count(b"\r\n") == text.count("\n")


def test_write_private_default_matches_the_platform_stream_and_raw_does_not(
    tmp_path: Path,
) -> None:
    text = "{\r\n}\r\n"
    default = tmp_path / "default.json"
    raw = tmp_path / "nested" / "raw.json"
    translated = tmp_path / "translated.json"
    write_private(default, text)
    write_private(raw, text, newline="")
    write_private(translated, text, newline="\r\n")
    assert default.read_bytes() == _text_stream(tmp_path / "platform.json", text, None)
    assert raw.read_bytes() == text.encode()
    assert translated.read_bytes() == _text_stream(tmp_path / "stream.json", text, "\r\n")
    assert translated.read_bytes() != raw.read_bytes()
    assert _mode(default) == 0o600
    assert _mode(raw) == 0o600 and _mode(raw.parent) == 0o700
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_token_config_and_set_host_follow_the_platform_stream(tmp_path: Path) -> None:
    token = tmp_path / "token"
    write_token("secret-token", token)
    assert token.read_bytes() == _text_stream(tmp_path / "token-platform", "secret-token\n", None)
    assert _mode(token) == 0o600
    cfg = tmp_path / "config.json"
    write_config(cfg, url="https://h", namespace="ns", org="org")
    cfg_text = cfg.read_bytes().decode("utf-8")
    assert cfg.read_bytes() == _text_stream(tmp_path / "cfg-platform", cfg_text, None)
    assert _text_stream(tmp_path / "cfg-crlf", cfg_text, "\r\n") != cfg.read_bytes()
    assert _mode(cfg) == 0o600
    env = tmp_path / ".env"
    env.write_bytes(b"LOOM_JWT_SECRET=s3cret\n")
    ns = argparse.Namespace(
        emails=["ops@example.com"], env_file=str(env), append=False, dry_run=False
    )
    assert shell._cmd_set_host(ns) == 0
    env_text = env.read_bytes().decode("utf-8")
    assert b"LOOM_JWT_SECRET=s3cret\n" in env.read_bytes()
    assert env.read_bytes() == _text_stream(tmp_path / "env-platform", env_text, None)
    assert _text_stream(tmp_path / "env-crlf", env_text, "\r\n") != env.read_bytes()
    assert _mode(env) == 0o600
