"""dev/apparmor_check.sh, which redeploy.sh runs before it builds a colour:
go when the compose file's AppArmor profile is loaded, stop when it is not or
the host has no AppArmor. Driven against a fake securityfs directory."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "dev" / "apparmor_check.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _apparmor_fs(tmp_path: Path, names: list[str]) -> Path:
    fs = tmp_path / "apparmor"
    for i, name in enumerate(names):
        profile = fs / "policy" / "profiles" / f"{name[:32]}.{i}"
        profile.mkdir(parents=True)
        (profile / "name").write_text(name + "\n")
    fs.mkdir(exist_ok=True)
    return fs


def _compose(tmp_path: Path, *options: str) -> Path:
    path = tmp_path / "compose.yml"
    lines = ["x-common: &common", "  security_opt:", *(f"    - {o}" for o in options)]
    path.write_text("\n".join([*lines, "services:", "  web:", "    <<: *common", ""]))
    return path


def _run(compose: Path, fs: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "LOOM_APPARMOR_FS": str(fs)}
    return subprocess.run(
        ["bash", str(SCRIPT), str(compose)], capture_output=True, text=True, env=env, timeout=30
    )


def test_a_loaded_profile_passes_among_many(tmp_path: Path) -> None:
    # More than a pipe buffer of names after the match: `cat | grep -q` would
    # fail here under pipefail, cat taking SIGPIPE when grep exits early.
    filler = [f"profile-{i}-" + "x" * 240 for i in range(300)]
    fs = _apparmor_fs(tmp_path, ["loom-repo-sandbox", *filler])
    compose = _compose(tmp_path, "apparmor=loom-repo-sandbox", "systempaths=unconfined")
    done = _run(compose, fs)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_profile_not_loaded_stops_with_the_load_command(tmp_path: Path) -> None:
    fs = _apparmor_fs(tmp_path, ["docker-default", "loom-repo-sandbox-old"])
    done = _run(_compose(tmp_path, "apparmor=loom-repo-sandbox"), fs)
    assert done.returncode == 1
    assert "loom-repo-sandbox is not loaded" in done.stdout
    assert "apparmor_parser -r -W /etc/apparmor.d/loom-repo-sandbox" in done.stdout


def test_a_host_without_apparmor_stops(tmp_path: Path) -> None:
    done = _run(_compose(tmp_path, "apparmor=loom-repo-sandbox"), tmp_path / "absent")
    assert done.returncode == 1
    assert "this host has no AppArmor" in done.stdout


@pytest.mark.parametrize(
    "line",
    [
        '    - "apparmor=loom-repo-sandbox"',
        "    - 'apparmor:loom-repo-sandbox'",
        '  security_opt: ["seccomp=x.json", "apparmor=loom-repo-sandbox"]',
    ],
)
def test_every_compose_spelling_of_the_profile_is_checked(tmp_path: Path, line: str) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(f"services:\n  web:\n{line}\n")
    loaded = _run(compose, _apparmor_fs(tmp_path, ["loom-repo-sandbox"]))
    assert loaded.returncode == 0, loaded.stdout
    missing = _run(compose, _apparmor_fs(tmp_path / "other", ["docker-default"]))
    assert missing.returncode == 1 and "loom-repo-sandbox is not loaded" in missing.stdout


@pytest.mark.parametrize(
    "options",
    [
        ["apparmor=${PROFILE}"],
        ["apparmor=docker-default", "apparmor=${PROFILE}"],
        ["apparmor="],
    ],
)
def test_an_apparmor_option_it_cannot_read_stops(tmp_path: Path, options: list[str]) -> None:
    done = _run(_compose(tmp_path, *options), _apparmor_fs(tmp_path, ["docker-default"]))
    assert done.returncode == 1 and "cannot read the AppArmor profile" in done.stdout


def test_two_profiles_on_one_line_are_both_checked(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        'services:\n  web:\n    security_opt: ["apparmor=missing-one", apparmor=docker-default]\n'
    )
    done = _run(compose, _apparmor_fs(tmp_path, ["docker-default"]))
    assert done.returncode == 1 and "missing-one is not loaded" in done.stdout


def test_every_named_profile_must_be_loaded(tmp_path: Path) -> None:
    compose = _compose(tmp_path, "apparmor=docker-default")
    compose.write_text(compose.read_text() + "  w:\n    security_opt: [apparmor=loom-extra]\n")
    done = _run(compose, _apparmor_fs(tmp_path, ["docker-default"]))
    assert done.returncode == 1 and "loom-extra is not loaded" in done.stdout


@pytest.mark.parametrize("options", [(), ("apparmor=unconfined",), ("seccomp=unconfined",)])
def test_no_named_profile_needs_nothing(tmp_path: Path, options: tuple[str, ...]) -> None:
    assert _run(_compose(tmp_path, *options), tmp_path / "absent").returncode == 0


def test_the_colour_compose_names_the_shipped_profile() -> None:
    colour = ROOT / "dev" / "docker-compose.colour.yml"
    assert "- apparmor=loom-repo-sandbox" in colour.read_text(encoding="utf-8")
    assert (ROOT / "deploy" / "apparmor" / "loom-repo-sandbox").is_file()


def test_redeploy_runs_the_check_before_the_preflight() -> None:
    text = (ROOT / "redeploy.sh").read_text(encoding="utf-8")
    check = text.index('dev/apparmor_check.sh "$COLOUR_COMPOSE" || exit 1')
    assert check < text.index("dev/redeploy_preflight.sh")


def test_redeploy_probes_the_new_colour_between_health_and_the_swap() -> None:
    text = (ROOT / "redeploy.sh").read_text(encoding="utf-8")
    probe = text.index('dev/sandbox_probe.sh "$idle" || {')
    assert text.index('wait_workers "$idle"') < probe < text.index('swap_to "$idle"')
    # --rollback refuses a colour whose containers are gone (the probe removes them)
    rollback = text[text.index("rollback() {") : text.index("# ---- arguments")]
    assert "the previous build is gone" in rollback


_STUB_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$1" in
  inspect)
    [[ "$STUB_INSPECT" == fail ]] && { echo "Error: No such object: ${@: -1}" >&2; exit 1; }
    case "$STUB_INSPECT" in
      sandbox) echo "[seccomp={} apparmor=loom-repo-sandbox systempaths=unconfined]" ;;
      last) echo "[seccomp={} apparmor=loom-repo-sandbox]" ;;
      suffixed) echo "[apparmor=loom-repo-sandbox-old]" ;;
      *) echo "[]" ;;
    esac
    ;;
  exec) exit "$STUB_PROBE" ;;
esac
"""


def _probe(tmp_path: Path, inspect: str, probe: int) -> tuple[int, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(_STUB_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_INSPECT": inspect,
        "STUB_PROBE": str(probe),
    }
    done = subprocess.run(
        ["bash", str(ROOT / "dev" / "sandbox_probe.sh"), "green"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return done.returncode, log.read_text().splitlines()


_RM = "rm -f loom-green loom-green-workers"


@pytest.mark.parametrize(
    ("inspect", "probe", "rc", "execs", "removed"),
    [
        # an inspection error is a failure, not "not under the profile"
        ("fail", 0, 1, 0, True),
        ("plain", 0, 0, 0, False),  # not under the profile: nothing to probe
        ("sandbox", 1, 1, 1, True),  # the first container fails: stop there
        ("sandbox", 0, 0, 2, False),  # both containers probed and pass
        ("last", 1, 1, 1, True),  # the profile as the last option
        ("suffixed", 1, 0, 0, False),  # another profile whose name only starts the same
    ],
)
def test_the_sandbox_probe_gates_the_swap(
    tmp_path: Path, inspect: str, probe: int, rc: int, execs: int, removed: bool
) -> None:
    code, calls = _probe(tmp_path, inspect, probe)
    assert code == rc
    assert sum(c.startswith("exec ") for c in calls) == execs
    assert (_RM in calls) is removed
