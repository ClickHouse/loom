"""install.sh with a local bundle and LOOM_SKIP_START=1: the files and the
.env it writes for each ClickHouse target. No network, no containers; a stub
`docker` on PATH answers the version check."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _build(tmp: Path) -> tuple[Path, Path]:
    path = ROOT / "scripts" / "release" / "build_stack_bundle.py"
    spec = importlib.util.spec_from_file_location("build_stack_bundle", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build("9.9.9", tmp / "bundle", "clickhouse/clickhouse-server:26.9.18.11")


@pytest.fixture
def install(tmp_path: Path):
    archive, checksum = _build(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\n[ "$1 $2 $3" = "compose version --short" ] && echo 2.29.1\n')
    docker.chmod(0o755)
    target = tmp_path / "loom"

    def run(
        *args: str, start_new_session: bool = False, **env: str
    ) -> subprocess.CompletedProcess[str]:
        full_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "LOOM_INSTALL_DIR": str(target),
            "LOOM_INSTALL_BUNDLE": str(archive),
            "LOOM_INSTALL_CHECKSUM": str(checksum),
            "LOOM_SKIP_START": "1",
            **env,
        }
        return subprocess.run(
            ["sh", str(ROOT / "install.sh"), *args],
            env=full_env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=start_new_session,  # no controlling terminal
            check=False,
        )

    return run, target


def _env(target: Path) -> dict[str, str]:
    lines = (target / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_a_default_install_runs_the_bundled_clickhouse(install) -> None:
    run, target = install
    result = run()
    assert result.returncode == 0, result.stderr
    assert (target / "compose.yaml").is_file()
    assert (target / "deploy" / "clickhouse-config" / "nlp_user_settings.xml").is_file()
    assert (target / "deploy" / "lemmagen3" / "en.bin").is_file()
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == "bundled"
    assert "LOOM_CH_HOST" not in env
    assert stat.S_IMODE((target / ".env").stat().st_mode) == 0o600


def test_an_outside_clickhouse_is_written_to_env_and_never_printed(install) -> None:
    run, target = install
    url = "https://loom%40acme:p%40ss$w0rd%3A%2F@abc.clickhouse.cloud:9440/ignored"
    result = run("--clickhouse", url)
    assert result.returncode == 0, result.stderr
    assert "p@ss" not in result.stdout + result.stderr
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == ""
    assert env["LOOM_CH_HOST"] == "abc.clickhouse.cloud"
    assert env["LOOM_CH_PORT"] == "9440"
    assert env["LOOM_CH_SECURE"] == "true"
    assert env["LOOM_CH_USER"] == "loom@acme"
    # Single quotes: Compose takes the value literally, `$` included.
    assert env["LOOM_CH_PASSWORD"] == "'p@ss$w0rd:/'"
    assert stat.S_IMODE((target / ".env").stat().st_mode) == 0o600


def test_the_password_can_come_from_the_environment(install) -> None:
    run, target = install
    result = run(LOOM_CH_URL="http://loom@ch.internal", LOOM_CH_PASSWORD="from-env")
    assert result.returncode == 0, result.stderr
    env = _env(target)
    assert (env["LOOM_CH_HOST"], env["LOOM_CH_PORT"], env["LOOM_CH_SECURE"]) == (
        "ch.internal",
        "8123",
        "false",
    )
    assert env["LOOM_CH_USER"] == "loom" and env["LOOM_CH_PASSWORD"] == "'from-env'"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://h.example:8443", "names no user"),
        ("http://loom:pw@localhost:8123", "is the container itself"),
        ("http://loom:pw@127.0.0.1:8123", "is the container itself"),
    ],
)
def test_a_url_without_a_user_or_on_loopback_is_refused(install, url: str, message: str) -> None:
    run, target = install
    result = run("--clickhouse", url)
    assert result.returncode == 1 and message in result.stderr
    assert not (target / ".env").exists()


def test_no_password_and_no_terminal_says_where_to_put_it(install) -> None:
    run, target = install
    result = run("--clickhouse", "https://loom@h.example", start_new_session=True)
    assert result.returncode == 1
    assert "no ClickHouse password: set LOOM_CH_PASSWORD" in result.stderr


def test_a_password_with_a_single_quote_is_refused(install) -> None:
    run, target = install
    result = run("--clickhouse", "https://u:it's@h:8443")
    assert result.returncode == 1
    assert "single quote" in result.stderr
    assert not (target / ".env").exists()


def test_an_upgrade_keeps_env_and_an_older_install_stays_bundled(install) -> None:
    run, target = install
    assert run().returncode == 0
    env_path = target / ".env"
    lines = [line for line in env_path.read_text().splitlines() if "COMPOSE_PROFILES" not in line]
    env_path.write_text("\n".join(lines + ["LOOM_JWT_SECRET=kept"]) + "\n")
    (target / "clickhouse-config").mkdir()  # the layout before deploy/
    result = run()
    assert result.returncode == 0, result.stderr
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == "bundled"
    assert env["LOOM_JWT_SECRET"] == "kept" and env["LOOM_VERSION"] == "9.9.9"
    assert not (target / "clickhouse-config").exists()


def test_switching_an_install_to_an_outside_clickhouse(install) -> None:
    run, target = install
    assert run().returncode == 0
    switched = run("--clickhouse", "https://loom:pw@h.example")
    assert switched.returncode == 0
    assert "--profile bundled stop clickhouse" in switched.stdout
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == "" and env["LOOM_CH_HOST"] == "h.example"
    assert list(env).count("LOOM_CH_HOST") == 1
