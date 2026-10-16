"""The host cron scripts run end to end against a stub loom root: every step
leaves a STEP line with its exit code, a hung step is ended by its timeout,
an entry a full pass holds is skipped by name, and a failed entry listing
fails the pass instead of exiting silently."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DELTA = ROOT / "deploy" / "cron" / "scip-delta.sh"
FULL = ROOT / "deploy" / "cron" / "scip-full.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "flock", "timeout", "python3")),
    reason="needs bash, flock, timeout and python3",
)

# Stands in for .venv/bin/python: `-m server.cli code <cmd>` answers per
# command; `entries` lists the configured repositories; `entry-lock` takes
# `$STUB_LOCK_DIR/<namespace>.lock` (exit 75 when held) and runs the command.
_PYTHON = """#!/usr/bin/env bash
case "$4" in
  entry-lock)
    ns=$8
    shift 9
    exec 7>"$STUB_LOCK_DIR/$ns.lock"
    flock -n 7 || exit 75
    "$@" ;;
  entries)
    [ -n "${STUB_ENTRIES_FAIL:-}" ] && { echo "error: stub" >&2; exit 1; }
    printf '%s\\n' "$STUB_ENTRIES" ;;
  scip-delta) echo "RESULT: stub delta" ;;
  crossref) sleep "${STUB_CROSSREF_SLEEP:-0}"; echo "crossref $*" ;;
  scip-full) echo "full $*"; exit "${STUB_FULL_RC:-0}" ;;
esac
"""
_HARNESS = """#!/usr/bin/env bash
echo "harness key=${LOOM_API_KEY:+set} args=$*"
"""


def _root(tmp_path: Path, entries: list[str]) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "loom"
    for rel, body in (
        (".venv/bin/python", _PYTHON),
        ("scripts/scip_harness/run_python_delta.sh", _HARNESS),
    ):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
        f.chmod(0o755)
    (root / "var" / "cron").mkdir(parents=True)
    (root / "var" / "cron" / "loom_writer_token.json").write_text('{"token": "t0k"}')
    (root / "checkout").mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "LOOM_ROOT": str(root),
        "STUB_ENTRIES": "\n".join(entries),
        "STUB_LOCK_DIR": str(tmp_path),
    }
    return root, env


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
    )


def _steps(log: Path) -> list[str]:
    return [ln for ln in log.read_text().splitlines() if ln.startswith("STEP ")]


def test_delta_writes_a_step_line_per_step_and_passes_the_writer_token(tmp_path: Path) -> None:
    root, env = _root(tmp_path, [f"acme code-a checkout {tmp_path}/a.lock"])
    assert _run(DELTA, env).returncode == 0
    log = root / "var" / "cron" / "scip-delta.log"
    steps = [s.rsplit(" seconds=", 1)[0] for s in _steps(log)]
    assert steps == [
        "STEP scip-delta ns=all exit=0",
        "STEP crossref ns=code-a exit=0",
        "STEP python-scip ns=code-a exit=0",
    ]
    text = log.read_text()
    # relative checkout resolved against the loom root; token reached the harness only
    assert f"harness key=set args={root}/checkout http://localhost:7777 acme code-a" in text
    assert "t0k" not in text


def test_delta_step_timeout_ends_a_hung_step(tmp_path: Path) -> None:
    root, env = _root(tmp_path, [f"acme code-a checkout {tmp_path}/a.lock"])
    env |= {"STUB_CROSSREF_SLEEP": "30", "SCIP_DELTA_CROSSREF_TIMEOUT": "1s"}
    assert _run(DELTA, env).returncode == 0
    steps = _steps(root / "var" / "cron" / "scip-delta.log")
    assert any(s.startswith("STEP crossref ns=code-a exit=124 ") for s in steps)
    assert any(s.startswith("STEP python-scip ns=code-a exit=0 ") for s in steps)


def test_delta_skips_only_the_entry_a_full_pass_holds(tmp_path: Path) -> None:
    held = tmp_path / "code-a.lock"
    root, env = _root(
        tmp_path, [f"acme code-a checkout {held}", f"acme code-b checkout {tmp_path}/code-b.lock"]
    )
    with held.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _run(DELTA, env).returncode == 0
    steps = [s.rsplit(" seconds=", 1)[0] for s in _steps(root / "var" / "cron" / "scip-delta.log")]
    assert "STEP entry-lock ns=code-a exit=0" in steps
    assert "STEP crossref ns=code-b exit=0" in steps
    assert not any("crossref ns=code-a" in s for s in steps)


@pytest.mark.parametrize("script", [DELTA, FULL], ids=["delta", "full"])
def test_a_failed_entry_listing_fails_the_pass_with_a_line(tmp_path: Path, script: Path) -> None:
    root, env = _root(tmp_path, [])
    env["STUB_ENTRIES_FAIL"] = "1"
    assert _run(script, env).returncode == 1
    log = root / "var" / "cron" / script.name.replace(".sh", ".log")
    assert "STEP entries ns=all exit=1 seconds=0 reason=entry-list-failed" in log.read_text()
    assert "error: stub" in log.read_text(), "the command's stderr lands in the log"


def test_full_records_each_entry_exit_and_goes_on(tmp_path: Path) -> None:
    root, env = _root(tmp_path, ["acme code-a checkout", "acme code-b missing"])
    env["STUB_FULL_RC"] = "3"
    assert _run(FULL, env).returncode == 0
    log = root / "var" / "cron" / "scip-full.log"
    steps = [s.rsplit(" seconds=", 1)[0] for s in _steps(log)]
    assert steps == ["STEP scip-full ns=code-a exit=3"]
    assert "skip code-b: no checkout at missing" in log.read_text()
    assert not (root / "var" / "cron" / ".f.tmp").exists()
