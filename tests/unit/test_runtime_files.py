"""`server.core.runtime_files`: per-server files in the runtime directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from server.core import runtime_files
from server.core.runtime_files import server_file

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(runtime_files, "_pruned", set())
    return tmp_path / "loom"


def test_a_name_carries_the_parent_and_its_start_time(run_dir: Path) -> None:
    started = runtime_files._started(os.getppid())
    assert server_file("budgets") == str(run_dir / f"loom-budgets-{os.getppid()}-{started}")


def test_files_of_servers_no_longer_running_are_removed(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = Path(server_file("budgets") + ".json")
    live.write_text("{}")
    stale = run_dir / "loom-ratelimit-overrides-1-999999999.json"  # pid 1 started at another time
    stale.write_text("{}")
    (run_dir / "loom-graph-marks-1-999999999").mkdir()
    unrelated = run_dir / "notes.txt"
    unrelated.write_text("")
    monkeypatch.setattr(runtime_files, "_pruned", set())  # the next name prunes again
    server_file("budgets")
    assert sorted(p.name for p in run_dir.iterdir()) == sorted([live.name, unrelated.name])


def test_a_directory_is_pruned_once_per_process(run_dir: Path) -> None:
    server_file("x")
    stale = run_dir / "loom-budgets-1-999999999.json"
    stale.write_text("{}")
    server_file("x")
    assert stale.exists()


def test_nothing_is_removed_where_proc_cannot_say(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_file("x")
    stale = run_dir / "loom-budgets-1-999999999.json"
    stale.write_text("{}")
    monkeypatch.setattr(runtime_files, "_pruned", set())
    monkeypatch.setattr(runtime_files.os.path, "isdir", lambda p: p != "/proc")
    server_file("x")
    assert stale.exists()
