"""`client:node`: the doctor runs the installed plugin's own launcher, so it
names the Node the hooks and the MCP header helper get, or why there is none."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import server.cli.doctor_client as dc

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "integrations" / "claude-code-plugin" / "bin" / "loom-node"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home whose GUI PATH and fixed directories hold no Node of the host's.
    The settings `env` applies to both launcher runs, as it does to the hooks."""
    monkeypatch.setattr(dc, "GUI_PATH", str(tmp_path / "gui-path"))
    _settings(tmp_path, {})
    return tmp_path


def _settings(home: Path, env: dict[str, str]) -> None:
    (home / ".claude").mkdir(exist_ok=True)
    isolated = {"LOOM_NODE_SYSTEM_DIRS": ""}
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {**isolated, **env}}))


def _install(home: Path, client: str, version: str) -> Path:
    bin_dir = home / client / "plugins" / "cache" / "loom" / "loom" / version / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(LAUNCHER, bin_dir / "loom-node")
    return bin_dir / "loom-node"


def _node(directory: Path, version: str) -> Path:
    directory.mkdir(parents=True)
    node = directory / "node"
    node.write_text(f"#!/bin/sh\necho {version}\n")
    node.chmod(0o755)
    return node


def _check(home: Path, **env: str) -> tuple[str, str]:
    [(name, grade, line)] = dc.node_check(home, {"HOME": str(home), "PATH": "/nowhere", **env})
    assert name == "client:node"
    return grade, line


def test_no_installed_launcher_no_line(home: Path) -> None:
    assert dc.node_check(home, {"HOME": str(home), "PATH": "/usr/bin:/bin"}) == []


def test_the_newest_installed_plugin_answers(home: Path) -> None:
    _install(home, ".claude", "1.2.9")
    newest = _install(home, ".codex", "1.2.10")
    assert dc.newest_launcher(home) == newest


def test_a_version_manager_node_passes_without_the_shell_path(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    node = _node(home / ".nvm" / "versions" / "node" / "v22.1.0" / "bin", "v22.1.0")
    assert _check(home) == (dc.PASS, f"Loom hook runtime ready: {node} (v22.1.0)")


def test_a_node_only_on_the_shell_path_warns(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    node = _node(home / "shell-only", "v24.0.0")
    grade, line = _check(home, PATH=str(node.parent))
    assert grade == dc.WARN
    assert line.startswith(f"Loom hook runtime ready: {node} (v24.0.0), but only with this shell")
    assert "set LOOM_NODE to that path" in line


def test_shell_only_variables_do_not_reach_the_gui_run(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    node = _node(home / "custom-nvm" / "versions" / "node" / "v22.1.0" / "bin", "v22.1.0")
    assert _check(home, LOOM_NODE=str(node))[0] == dc.WARN
    assert _check(home, NVM_DIR=str(home / "custom-nvm"))[0] == dc.WARN


def test_loom_node_in_claude_settings_counts(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    node = _node(home / "shell-only", "v24.0.0")
    _settings(home, {"LOOM_NODE": str(node)})
    assert _check(home)[0] == dc.PASS


def test_no_node_fails_with_the_launcher_line_and_the_fix(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    old = _node(home / ".volta" / "bin", "v16.20.2")
    grade, line = _check(home)
    assert grade == dc.FAIL
    assert line.startswith(
        f"loom: Node.js 18+ is required; the first Node found, {old} (v16.20.2), is older."
    )
    assert "the MCP entry gets no token" in line and "LOOM_NODE=/path/to/node" in line


def test_settings_env_keeps_only_strings(home: Path) -> None:
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"LOOM_HOOK_TIMEOUT_MS": None, "LOOM_NODE": "/x", "N": 0}})
    )
    assert dc._claude_settings_env(home) == {"LOOM_NODE": "/x"}
    assert dc.hook_override_check(home, {}) == []


def test_codex_hooks_do_not_get_claude_settings_env(home: Path) -> None:
    _install(home, ".codex", "1.3.0")
    node = _node(home / "elsewhere", "v24.0.0")
    _settings(home, {"LOOM_NODE": str(node)})
    grade, line = _check(home, LOOM_NODE_SYSTEM_DIRS="")
    assert grade == dc.FAIL
    assert line.startswith("loom: Node.js 18+ not found")


def test_a_launcher_that_does_not_run_is_a_warning_not_a_verdict(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _install(home, ".claude", "1.3.0")

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", refuse)
    assert _check(home) == (dc.WARN, f"could not run {launcher}: [Errno 13] Permission denied")
