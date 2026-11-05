"""`client:node`: the doctor runs the installed plugin's own launcher, so it
names the Node the hooks and the MCP header helper get, or why there is none."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import server.cli.doctor_client as dc

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "integrations" / "claude-code-plugin" / "bin" / "loom-node"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


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


def _env(home: Path, path: str) -> dict[str, str]:
    return {"HOME": str(home), "PATH": path, "LOOM_NODE_SYSTEM_DIRS": ""}


def test_no_installed_launcher_no_line(tmp_path: Path) -> None:
    assert dc.node_check(tmp_path, _env(tmp_path, "/usr/bin:/bin"), dc.run_launcher) == []


def test_the_newest_installed_plugin_answers(tmp_path: Path) -> None:
    _install(tmp_path, ".claude", "1.2.9")
    newest = _install(tmp_path, ".codex", "1.2.10")
    assert dc.newest_launcher(tmp_path) == newest


def test_a_version_manager_node_passes_without_the_shell_path(tmp_path: Path) -> None:
    _install(tmp_path, ".claude", "1.3.0")
    node = _node(tmp_path / ".nvm" / "versions" / "node" / "v22.1.0" / "bin", "v22.1.0")
    [(name, grade, line)] = dc.node_check(
        tmp_path, _env(tmp_path, "/usr/bin:/bin"), dc.run_launcher
    )
    assert (name, grade) == ("client:node", dc.PASS)
    assert line == f"Loom hook runtime ready: {node} (v22.1.0)"


def test_a_node_only_on_the_shell_path_warns_and_names_loom_node(tmp_path: Path) -> None:
    _install(tmp_path, ".claude", "1.3.0")
    node = _node(tmp_path / "shell-only", "v24.0.0")
    [(_, grade, line)] = dc.node_check(
        tmp_path, _env(tmp_path, f"{node.parent}:/usr/bin:/bin"), dc.run_launcher
    )
    assert grade == dc.WARN
    assert f"Fix: LOOM_NODE={node} in the agent's environment" in line


def test_loom_node_in_claude_settings_counts(tmp_path: Path) -> None:
    _install(tmp_path, ".claude", "1.3.0")
    node = _node(tmp_path / "shell-only", "v24.0.0")
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"LOOM_NODE": str(node)}})
    )
    [(_, grade, _)] = dc.node_check(
        tmp_path, _env(tmp_path, f"{node.parent}:/usr/bin:/bin"), dc.run_launcher
    )
    assert grade == dc.PASS


def test_no_node_fails_with_the_launcher_line_and_the_fix(tmp_path: Path) -> None:
    _install(tmp_path, ".claude", "1.3.0")
    _node(tmp_path / ".volta" / "bin", "v16.20.2")
    [(_, grade, line)] = dc.node_check(tmp_path, _env(tmp_path, "/nowhere"), dc.run_launcher)
    assert grade == dc.FAIL
    assert line.startswith("loom: Node.js 18+ is required; the only Node found is ")
    assert "(v16.20.2)" in line
    assert "the MCP entry gets no token" in line and "LOOM_NODE=/path/to/node" in line
