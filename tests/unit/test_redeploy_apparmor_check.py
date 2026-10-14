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


def test_redeploy_probes_both_new_containers_before_the_swap_and_removes_them_on_failure() -> None:
    text = (ROOT / "redeploy.sh").read_text(encoding="utf-8")
    loop = text.index('for c in "loom-$idle" "loom-$idle-workers"; do')
    block = text[loop : text.index("done", loop)]
    # decided from the running container, not the compose text (comments included)
    assert "{{.HostConfig.SecurityOpt}}" in block and "*apparmor=loom-repo-sandbox*" in block
    assert 'docker exec "$c" python -c' in block and "s.sandbox_available()" in block
    # removed, not stopped: the up already replaced the previous build in that colour, and
    # --rollback starts a stopped colour without probing it
    assert 'docker rm -f "loom-$idle" "loom-$idle-workers"' in block and "docker stop" not in block
    rollback = text[text.index("rollback() {") : text.index("# ---- arguments")]
    assert "the previous build is gone" in rollback
    assert text.index('wait_workers "$idle"') < loop < text.index('swap_to "$idle"')
