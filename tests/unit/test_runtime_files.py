"""`server.core.runtime_files`: per-server files in the runtime directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from server.core import runtime_files
from server.core.runtime_files import server_file

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")

NS = runtime_files._pid_namespace()
DEAD = f"{NS}-1-999999999"  # pid 1 of this namespace, started at another time


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(runtime_files, "_pruned", set())
    return tmp_path / "loom"


def _prune_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_files, "_pruned", set())


def test_a_name_carries_the_namespace_the_parent_and_its_start_time(run_dir: Path) -> None:
    ppid = os.getppid()
    expected = f"loom-budgets-{NS}-{ppid}-{runtime_files._started(ppid)}"
    assert server_file("budgets") == str(run_dir / expected)


def test_files_of_servers_gone_from_this_namespace_are_removed(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = Path(server_file("budgets") + ".json")
    live.write_text("{}")
    (run_dir / f"loom-ratelimit-overrides-{DEAD}.json").write_text("{}")
    (run_dir / f"loom-graph-marks-{DEAD}").mkdir()
    unrelated = run_dir / "notes.txt"
    unrelated.write_text("")
    _prune_again(monkeypatch)
    server_file("budgets")
    assert sorted(p.name for p in run_dir.iterdir()) == sorted([live.name, unrelated.name])


def test_files_of_another_pid_namespace_are_kept(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another container sharing the directory has its own pid 1."""
    server_file("x")
    theirs = run_dir / "loom-budgets-1-1-999999999.json"
    theirs.write_text("{}")
    _prune_again(monkeypatch)
    server_file("x")
    assert theirs.exists()


def test_a_directory_is_pruned_once_per_process(run_dir: Path) -> None:
    server_file("x")
    stale = run_dir / f"loom-budgets-{DEAD}.json"
    stale.write_text("{}")
    server_file("x")
    assert stale.exists()


def test_nothing_is_removed_when_the_namespace_is_unknown(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_file("x")
    stale = run_dir / f"loom-budgets-{DEAD}.json"
    stale.write_text("{}")
    _prune_again(monkeypatch)
    monkeypatch.setattr(runtime_files, "_pid_namespace", lambda: "")
    server_file("x")
    assert stale.exists()
