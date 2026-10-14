"""scripts/serve.sh, the image's launcher, with stub `python` and `uvicorn` on
PATH: how many web processes it starts and which runtime directory they share."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_PYTHON = """#!/bin/sh
case "$*" in
  "-m server.core.web_workers") echo 4 ;;
  "-m server.core.runtime_files")
    [ "$STUB_RUNTIME_DIR" = none ] && { echo "loom: no private directory" >&2; exit 2; }
    [ "$STUB_RUNTIME_DIR" = broken ] && { echo "Traceback: settings invalid" >&2; exit 1; }
    echo "${LOOM_RUNTIME_DIR:-$STUB_RUNTIME_DIR}" ;;
  *) exit 99 ;;
esac
"""
_UVICORN = """#!/bin/sh
echo "$* runtime=${LOOM_RUNTIME_DIR:-}"
"""


def _launch(
    tmp_path: Path, *, workers: str, runtime_dir: str, pinned: str = ""
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
        "PORT": "1",
    }
    if pinned:
        env["LOOM_RUNTIME_DIR"] = pinned
    return subprocess.run(
        ["sh", str(ROOT / "scripts" / "serve.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _workers(out: str) -> str:
    args = out.split()
    return args[args.index("--workers") + 1]


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
