"""scripts/serve.sh, the image's launcher, with stub `python` and `uvicorn` on
PATH: how many web processes it starts, which runtime directory they share and
which event loop they run, and the bind address the startup checks read."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_PYTHON = """#!/bin/sh
case "$*" in
  "-m server.core.web_workers") echo 4 ;;
  "-m server.settings.launch runtime_dir web_workers trusted_proxies")
    echo "launch_runtime_dir='${STUB_LAUNCH_RUNTIME_DIR:-}'"
    echo "launch_web_workers='$STUB_LAUNCH_WORKERS'" ;;
  "-m server.core.runtime_files")
    [ "$STUB_RUNTIME_DIR" = none ] && { echo "loom: no private directory" >&2; exit 2; }
    [ "$STUB_RUNTIME_DIR" = broken ] && { echo "Traceback: settings invalid" >&2; exit 1; }
    echo "${LOOM_RUNTIME_DIR:-$STUB_RUNTIME_DIR}" ;;
  *) exit 99 ;;
esac
"""
_UVICORN = """#!/bin/sh
echo "$* bind=${LOOM_BIND_HOST:-} runtime=${LOOM_RUNTIME_DIR:-}"
"""


def _launch(
    tmp_path: Path,
    *,
    workers: str,
    runtime_dir: str,
    pinned: str = "",
    resolved_workers: str = "",
    resolved_runtime_dir: str = "",
    loop: str = "",
    extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("python", _PYTHON), ("uvicorn", _UVICORN)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_RUNTIME_DIR": runtime_dir,
        "LOOM_WEB_WORKERS": workers,
        "STUB_LAUNCH_WORKERS": resolved_workers,
        "STUB_LAUNCH_RUNTIME_DIR": resolved_runtime_dir,
        "PORT": "1",
    }
    if loop:
        env["LOOM_UVICORN_LOOP"] = loop
    if pinned:
        env["LOOM_RUNTIME_DIR"] = pinned
    env.update(extra or {})
    return subprocess.run(
        ["sh", str(ROOT / "scripts" / "serve.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _arg(out: str, flag: str) -> str:
    args = out.split()
    return args[args.index(flag) + 1]


def _workers(out: str) -> str:
    return _arg(out, "--workers")


@pytest.mark.parametrize(
    ("workers", "runtime_dir", "started", "shared"),
    [
        ("", "/run/loom", "4", "/run/loom"),  # the count it chose, with a directory
        ("", "none", "1", ""),  # chosen, nowhere to share: one process
        ("3", "/run/loom", "3", "/run/loom"),
        ("1", "none", "1", ""),  # one process looks for no directory
    ],
)
def test_the_launcher_starts_what_it_can_share(
    tmp_path: Path, workers: str, runtime_dir: str, started: str, shared: str
) -> None:
    done = _launch(tmp_path, workers=workers, runtime_dir=runtime_dir)
    assert done.returncode == 0, done.stderr
    assert _workers(done.stdout) == started
    assert done.stdout.strip().endswith(f"runtime={shared}")
    if workers == "" and runtime_dir == "none":
        assert "starting one web process" in done.stderr


@pytest.mark.parametrize(("workers", "started"), [("", "2"), ("3", "3")])
def test_a_count_only_a_file_names_is_kept(tmp_path: Path, workers: str, started: str) -> None:
    """The server reads .env and the settings file, so the launcher takes their
    count too; the environment still wins."""
    done = _launch(tmp_path, workers=workers, runtime_dir="/run/loom", resolved_workers="2")
    assert done.returncode == 0, done.stderr
    assert _workers(done.stdout) == started


@pytest.mark.parametrize(
    ("env", "bind"),
    [
        ({"LOOM_LISTEN_HOST": "0.0.0.0"}, "0.0.0.0"),  # checked as reachable
        ({"LOOM_LISTEN_HOST": "0.0.0.0", "LOOM_BIND_HOST": "127.0.0.1"}, "127.0.0.1"),
        ({"LOOM_LISTEN_HOST": "::"}, "::"),  # the IPv6 wildcard
        ({"LOOM_LISTEN_HOST": "127.0.0.1"}, ""),  # the settings decide
        ({"LOOM_LISTEN_HOST": "::1"}, ""),
        ({"LOOM_LISTEN_HOST": "localhost"}, ""),
        ({}, ""),
    ],
)
def test_listening_beyond_loopback_is_what_the_bind_checks_read(
    tmp_path: Path, env: dict[str, str], bind: str
) -> None:
    """A listen address alone cannot skip the startup checks: the checks read
    LOOM_BIND_HOST, which defaults to a non-loopback listen address."""
    done = _launch(tmp_path, workers="1", runtime_dir="none", extra=env)
    assert done.returncode == 0, done.stderr
    assert f" bind={bind} " in done.stdout


def _without_loom() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}


def _resolved(name: str, **env: str) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "server.settings.launch", name],
        env={**_without_loom(), "LOOM_ENV_FILE": "", **env},
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    # One shell assignment per name asked for.
    assignment, value = done.stdout.strip().split("=", 1)
    assert assignment == f"launch_{name}"
    return shlex.split(value)[0]


def test_the_launch_values_resolve_as_the_server_loads_them(tmp_path: Path) -> None:
    env = tmp_path / "loom.env"
    env.write_text("LOOM_WEB_WORKERS=5\n")
    yaml = tmp_path / "loom.yaml"
    yaml.write_text("version: 1\nsettings:\n  web_workers: 3\n  trusted_proxies: 10.0.0.0/8\n")
    files = {"LOOM_ENV_FILE": str(env), "LOOM_SETTINGS_FILE": str(yaml)}
    assert _resolved("web_workers", **files) == "5"  # the env file over the settings file
    assert _resolved("trusted_proxies", **files) == "10.0.0.0/8"
    assert _resolved("web_workers", LOOM_SETTINGS_FILE=str(yaml)) == "3"
    assert _resolved("web_workers") == ""  # set nowhere: the launcher chooses
    assert _resolved("trusted_proxies") == ""


def _launch_real(tmp_path: Path, settings_file: str) -> subprocess.CompletedProcess[str]:
    """serve.sh with the real resolver and stub uvicorn and runtime check."""
    yaml = tmp_path / "loom.yaml"
    yaml.write_text(settings_file)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = f'''#!/bin/sh
case "$*" in
  "-m server.settings.launch "*) exec "{sys.executable}" "$@" ;;
  "-m server.core.runtime_files") echo /run/loom ;;
  *) exit 99 ;;
esac
'''
    for name, body in (("python", shim), ("uvicorn", _UVICORN)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = {**_without_loom(), "PATH": f"{bin_dir}:/usr/bin:/bin", "PORT": "1", "LOOM_ENV_FILE": ""}
    return subprocess.run(
        ["sh", str(ROOT / "scripts" / "serve.sh")],
        env={**env, "LOOM_SETTINGS_FILE": str(yaml)},
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_settings_file_sets_the_web_tier_uvicorn_starts(tmp_path: Path) -> None:
    """A count and proxies named only in the settings file reach uvicorn's arguments."""
    done = _launch_real(
        tmp_path, "version: 1\nsettings:\n  web_workers: 3\n  trusted_proxies: 10.0.0.0/8\n"
    )
    assert done.returncode == 0, done.stderr
    assert _workers(done.stdout) == "3"
    assert "--forwarded-allow-ips=10.0.0.0/8" in done.stdout.split()


def test_a_pin_it_cannot_use_refuses_even_one_process(tmp_path: Path) -> None:
    done = _launch(tmp_path, workers="1", runtime_dir="none", pinned="/data/rt")
    assert done.returncode == 2 and "not starting" in done.stderr


@pytest.mark.parametrize("pinned", ["", "/data/rt"])
def test_an_explicit_count_with_nowhere_to_share_refuses_to_start(
    tmp_path: Path, pinned: str
) -> None:
    """A pinned LOOM_RUNTIME_DIR is checked before the processes start, too."""
    done = _launch(tmp_path, workers="3", runtime_dir="none", pinned=pinned)
    assert done.returncode == 2 and done.stdout == ""
    assert "no private directory" in done.stderr and "not starting" in done.stderr


def test_a_pin_it_cannot_use_refuses_to_start_even_with_a_chosen_count(tmp_path: Path) -> None:
    """A pinned directory is the operator's choice: a typo or the wrong owner is
    refused rather than quietly costing them the extra processes."""
    done = _launch(tmp_path, workers="", runtime_dir="none", pinned="/data/rt")
    assert done.returncode == 2 and done.stdout == ""
    assert "not starting" in done.stderr


@pytest.mark.parametrize("workers", ["", "1"])
def test_a_pin_only_a_file_names_is_checked_too(tmp_path: Path, workers: str) -> None:
    """The server reads .env and the settings file, so a directory pinned there
    alone is refused, not skipped or traded for one process."""
    done = _launch(
        tmp_path, workers=workers, runtime_dir="none", resolved_runtime_dir="/nonexist/x"
    )
    assert done.returncode == 2 and done.stdout == ""
    assert "not starting" in done.stderr


def test_a_check_that_breaks_stops_the_launcher_whatever_the_count(tmp_path: Path) -> None:
    """Only "no directory" (exit 2) falls back; a broken check is not that."""
    done = _launch(tmp_path, workers="", runtime_dir="broken")
    assert done.returncode == 1 and done.stdout == ""
    assert "starting one web process" not in done.stderr


def test_a_usable_pinned_directory_is_shared_as_it_is(tmp_path: Path) -> None:
    done = _launch(tmp_path, workers="", runtime_dir="/run/loom", pinned="/data/rt")
    assert done.returncode == 0 and _workers(done.stdout) == "4"
    assert done.stdout.strip().endswith("runtime=/data/rt")


def test_the_launchers_check_refuses_a_pinned_directory_it_cannot_write(
    tmp_path: Path,
) -> None:
    """The real check: a worker keeps a pinned directory that turns read-only,
    but the launcher does not start several on one."""
    pinned = tmp_path / "rt"
    pinned.mkdir(mode=0o500)
    try:
        if os.geteuid() == 0:
            pytest.skip("root can write anywhere")
        done = subprocess.run(
            [sys.executable, "-m", "server.core.runtime_files"],
            env={**os.environ, "LOOM_RUNTIME_DIR": str(pinned)},
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 2 and "not writable" in done.stderr
    finally:
        pinned.chmod(0o700)


@pytest.mark.parametrize("workers", ["auto", "0", "00", "-1", "4x", "99999999999999999999"])
def test_a_worker_count_that_is_not_a_positive_number_is_refused_by_name(
    tmp_path: Path, workers: str
) -> None:
    done = _launch(tmp_path, workers=workers, runtime_dir="/run/loom")
    assert done.returncode == 2 and done.stdout == ""
    assert f"LOOM_WEB_WORKERS={workers} is not a positive whole number" in done.stderr


@pytest.mark.parametrize(("loop", "started"), [("", "asyncio"), ("uvloop", "uvloop")])
def test_the_launcher_runs_the_stdlib_loop_unless_told(
    tmp_path: Path, loop: str, started: str
) -> None:
    done = _launch(tmp_path, workers="1", runtime_dir="none", loop=loop)
    assert done.returncode == 0, done.stderr
    assert _arg(done.stdout, "--loop") == started


def test_a_settings_file_the_server_would_refuse_stops_the_launcher(tmp_path: Path) -> None:
    done = _launch_real(tmp_path, "version: 1\nsettings: false\n")
    assert done.returncode != 0 and done.stdout == ""
    assert "`settings` must be a mapping" in done.stderr


@pytest.mark.parametrize("proxies", ["10.0.0.0/8, 192.168.0.0/16", "*", "$(touch {probe}) 'a'"])
def test_a_value_with_shell_characters_reaches_uvicorn_as_written(
    tmp_path: Path, proxies: str
) -> None:
    probe = tmp_path / "pwned"
    proxies = proxies.format(probe=probe)
    quoted = proxies.replace("'", "''")
    done = _launch_real(
        tmp_path, f"version: 1\nsettings:\n  web_workers: 1\n  trusted_proxies: '{quoted}'\n"
    )
    assert done.returncode == 0, done.stderr
    assert f"--forwarded-allow-ips={proxies} bind=" in done.stdout
    assert not probe.exists()
