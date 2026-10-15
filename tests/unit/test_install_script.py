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
def installer_env(tmp_path: Path) -> dict[str, str]:
    """The environment install.sh runs in: a local bundle, a stub docker, no start."""
    archive, checksum = _build(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\n[ "$1 $2 $3" = "compose version --short" ] && echo 2.29.1\n')
    docker.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "LOOM_INSTALL_DIR": str(tmp_path / "loom"),
        "LOOM_INSTALL_BUNDLE": str(archive),
        "LOOM_INSTALL_CHECKSUM": str(checksum),
        "LOOM_SKIP_START": "1",
    }


@pytest.fixture
def install(installer_env: dict[str, str]):
    def run(
        *args: str, start_new_session: bool = False, **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(ROOT / "install.sh"), *args],
            env={**installer_env, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=start_new_session,  # no controlling terminal
            check=False,
        )

    return run, Path(installer_env["LOOM_INSTALL_DIR"])


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


def test_a_raw_slash_or_colon_in_the_password_is_never_printed(install) -> None:
    run, target = install
    result = run("--clickhouse", "https://loom:Secr3t/xy:12@h.example:8443")
    assert result.returncode == 0, result.stderr
    assert "Secr3t" not in result.stdout + result.stderr
    env = _env(target)
    assert (env["LOOM_CH_HOST"], env["LOOM_CH_PORT"]) == ("h.example", "8443")
    assert env["LOOM_CH_PASSWORD"] == "'Secr3t/xy:12'"


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


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("--clickhouse", "http://loom:@h.example"), {}),
        (("--clickhouse", "http://loom@h.example"), {"LOOM_CH_PASSWORD": ""}),
    ],
)
def test_an_empty_password_needs_no_terminal(install, args, env) -> None:
    run, target = install
    result = run(*args, start_new_session=True, **env)
    assert result.returncode == 0, result.stderr
    assert _env(target)["LOOM_CH_PASSWORD"] == "''"


def test_a_rerun_for_the_same_server_keeps_the_saved_password(install) -> None:
    run, target = install
    assert run("--clickhouse", "https://loom:first@h.example").returncode == 0
    rerun = run("--clickhouse", "https://loom@h.example", start_new_session=True)
    assert rerun.returncode == 0, rerun.stderr
    assert "Using the ClickHouse password saved in .env" in rerun.stdout
    assert _env(target)["LOOM_CH_PASSWORD"] == "'first'"
    other_port = run("--clickhouse", "https://loom@h.example:9440", start_new_session=True)
    assert other_port.returncode == 1 and "no ClickHouse password" in other_port.stderr
    other = run("--clickhouse", "https://someone@h.example", start_new_session=True)
    assert other.returncode == 1 and "no ClickHouse password" in other.stderr


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


def _answer_prompt(env: dict[str, str], url: str, answer: bytes) -> None:
    """Run the installer on a pseudo-terminal and type `answer` at the prompt."""
    import pty
    import select
    import time

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("sh", ["sh", str(ROOT / "install.sh"), "--clickhouse", url], env)
    seen = b""
    deadline = time.monotonic() + 20
    while b"password for" not in seen and time.monotonic() < deadline:
        if select.select([fd], [], [], 0.5)[0]:
            seen += os.read(fd, 1024)
    assert b"(Enter keeps the one saved in .env)" in seen, seen
    os.write(fd, answer)
    os.waitpid(pid, 0)


def test_enter_at_a_rerun_prompt_keeps_the_saved_password(install, installer_env) -> None:
    run, target = install
    assert run("--clickhouse", "https://loom:first@h.example").returncode == 0
    _answer_prompt(installer_env, "https://loom@h.example", b"\n")
    assert _env(target)["LOOM_CH_PASSWORD"] == "'first'"
    _answer_prompt(installer_env, "https://loom@h.example", b"second\n")
    assert _env(target)["LOOM_CH_PASSWORD"] == "'second'"
