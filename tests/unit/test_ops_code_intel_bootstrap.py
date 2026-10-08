"""scripts/ops/code_intel_bootstrap.sh against a fake interpreter and a fake
curl: an unknown argument runs nothing, a dry run runs only the read-only
preflight and never prints the admin token, and a real run hands the token
to curl on stdin and to the bench through LOOM_API_KEY, never in an argv."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "tok-SECRET-123"

FAKE = """#!/usr/bin/env bash
printf '%s|%s\\n' "$(basename "$0")" "$*" >>"$BOOT_LOG"
[[ -t 0 ]] || { [[ "$(basename "$0")" == curl ]] && cat >>"$BOOT_STDIN"; }
[[ "$(basename "$0")" == python ]] && printf 'env LOOM_API_KEY=%s\\n' "${LOOM_API_KEY:-}" >>"$BOOT_LOG"
case "$*" in *code_org*) echo org1 ;; *code_namespace*) echo code-ns ;; esac
exit 0
"""


@pytest.fixture
def fakes(tmp_path: Path) -> Path:
    for name in ("python", "curl"):
        (tmp_path / name).write_text(FAKE)
        (tmp_path / name).chmod(0o755)
    return tmp_path


def _boot(fakes: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str, str]:
    env = {
        **os.environ,
        "PATH": f"{fakes}:{os.environ['PATH']}",
        "PYTHON": str(fakes / "python"),
        "LOOM_BASE_URL": "http://loom.example",
        "LOOM_ADMIN_TOKEN": TOKEN,
        "OPENAI_API_KEY": "sk-x",
        "BOOT_LOG": str(fakes / "log"),
        "BOOT_STDIN": str(fakes / "stdin"),
    }
    env.pop("LOOM_API_KEY", None)
    r = subprocess.run(
        ["bash", str(ROOT / "scripts/ops/code_intel_bootstrap.sh"), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    log = (fakes / "log").read_text() if (fakes / "log").exists() else ""
    stdin = (fakes / "stdin").read_text() if (fakes / "stdin").exists() else ""
    return r, log, stdin


def test_an_unknown_argument_runs_nothing(fakes: Path) -> None:
    r, log, _ = _boot(fakes, "--dryrun")
    assert r.returncode == 2 and log == ""


def test_a_dry_run_reads_only_and_never_prints_the_token(fakes: Path) -> None:
    r, log, _ = _boot(fakes, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert TOKEN not in r.stdout + r.stderr
    assert "DRY: LOOM_API_KEY=<admin token>" in r.stdout and "DRY RUN" in r.stdout
    assert "server.cli code" not in log and "code.calibrate" not in log
    assert "curl|-fsS -o /dev/null http://loom.example/v1/health" in log


def test_a_real_run_keeps_the_token_out_of_every_argv(fakes: Path) -> None:
    r, log, stdin = _boot(fakes)
    assert r.returncode == 0, r.stderr
    argv_lines = [line for line in log.splitlines() if not line.startswith("env ")]
    assert not any(TOKEN in line for line in argv_lines)
    assert f"Authorization: Bearer {TOKEN}" in stdin
    assert log.count(f"env LOOM_API_KEY={TOKEN}") == 3  # the three bench gates
    assert "PASSED" in r.stdout
