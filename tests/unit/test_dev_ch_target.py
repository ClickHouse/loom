"""The dev/ ClickHouse tools act only on a target someone named.

backup_loom, restore_loom and verify_audit_chain resolve host, port, user,
database and TLS from their flags over the server's LOOM_CH_* settings, refuse
to run when no host is named anywhere, and never take or print the password
on the command line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev import _ch_target, backup_loom, restore_loom, verify_audit_chain  # noqa: E402
from server.settings import Settings  # noqa: E402


def _args(*argv: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    _ch_target.add_target_args(p)
    return p.parse_args(list(argv))


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Settings:
    for name in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE", "SECURE"):
        monkeypatch.delenv(f"LOOM_CH_{name}", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)  # no .env here
    return Settings()


def test_no_host_anywhere_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = _settings(monkeypatch, tmp_path)
    with pytest.raises(_ch_target.TargetError, match="--host or set LOOM_CH_HOST"):
        _ch_target.resolve_target(_args(), s)


def test_env_names_the_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = _settings(
        monkeypatch,
        tmp_path,
        LOOM_CH_HOST="ch.internal",
        LOOM_CH_PORT="8443",
        LOOM_CH_USER="ops",
        LOOM_CH_PASSWORD="s3cret",
        LOOM_CH_DATABASE="loom_x",
        LOOM_CH_TELEMETRY_DATABASE="loom_x_telemetry",
    )
    t = _ch_target.resolve_target(_args(), s)
    assert (t.host, t.port, t.user, t.database, t.secure) == (
        "ch.internal",
        8443,
        "ops",
        "loom_x",
        True,
    )
    assert t.password == "s3cret"
    assert "s3cret" not in t.describe() and "s3cret" not in repr(t)
    assert t.describe() == "ops@ch.internal:8443/loom_x (tls)"


def test_flags_override_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = _settings(monkeypatch, tmp_path, LOOM_CH_HOST="a", LOOM_CH_DATABASE="loom")
    t = _ch_target.resolve_target(_args("--host", "b", "--database", "drill", "--port", "9"), s)
    assert (t.host, t.database, t.port, t.secure) == ("b", "drill", 9, False)


def test_flag_host_alone_is_enough(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = _settings(monkeypatch, tmp_path)
    assert _ch_target.resolve_target(_args("--host", "localhost"), s).host == "localhost"


@pytest.mark.parametrize("module", [backup_loom, restore_loom, verify_audit_chain])
def test_no_tool_takes_a_password_flag(module, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as ei:
        module.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--password" not in out


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (backup_loom, ["--out-dir", "x"]),
        (restore_loom, ["--in-dir", "x"]),
        (verify_audit_chain, []),
    ],
)
def test_every_tool_exits_2_without_a_target(
    module, argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _settings(monkeypatch, tmp_path)
    monkeypatch.setattr("server.settings.settings", s)
    monkeypatch.chdir(tmp_path)
    assert module.main(argv) == 2
    assert not (tmp_path / "x").exists()


def test_an_unreachable_target_is_a_target_error() -> None:
    t = _ch_target.CHTarget(host="127.0.0.1", port=1, user="u", database="d", secure=False)
    with pytest.raises(_ch_target.TargetError, match=r"cannot connect to u@127\.0\.0\.1:1/d"):
        t.client(connect_timeout=1)
