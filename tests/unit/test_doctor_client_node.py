"""`client:<client>:node`: the doctor runs each installed plugin's own
launcher in that client's environment, so it names the Node the hooks and
the MCP header helper get, or why there is none."""

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
    """A home whose GUI PATH and fixed directories hold no Node of the host's:
    Claude Code's settings env and the Codex helper's env both carry
    LOOM_NODE_SYSTEM_DIRS=""."""
    monkeypatch.setattr(dc, "GUI_PATH", str(tmp_path / "gui-path"))
    _settings(tmp_path, {})
    return tmp_path


def _settings(home: Path, env: dict[str, str]) -> None:
    (home / ".claude").mkdir(exist_ok=True)
    settings = {"env": {"LOOM_NODE_SYSTEM_DIRS": "", **env}}
    (home / ".claude" / "settings.json").write_text(json.dumps(settings))


def _install(home: Path, client: str, version: str) -> Path:
    """The plugin cached, and installed (Claude Code) or enabled (Codex)."""
    bin_dir = home / client / "plugins" / "cache" / "loom" / "loom" / version / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(LAUNCHER, bin_dir / "loom-node")
    if client == ".claude":
        install = {"scope": "user", "installPath": str(bin_dir.parent)}
        record = {"version": 2, "plugins": {"loom@loom": [install]}}
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(record))
    else:
        (home / ".codex" / "config.toml").write_text('[plugins."loom@loom"]\nenabled = true\n')
    return bin_dir / "loom-node"


def _node(directory: Path, version: str) -> Path:
    directory.mkdir(parents=True)
    node = directory / "node"
    node.write_text(f"#!/bin/sh\necho {version}\n")
    node.chmod(0o755)
    return node


def _checks(home: Path, **env: str) -> dict[str, tuple[str, str]]:
    base = {"HOME": str(home), "PATH": "/nowhere", "LOOM_NODE_SYSTEM_DIRS": ""}
    return {name: (grade, line) for name, grade, line in dc.node_checks(home, {**base, **env})}


def test_no_installed_launcher_no_line(home: Path) -> None:
    assert _checks(home) == {}


def test_the_newest_codex_install_answers(home: Path) -> None:
    _install(home, ".codex", "1.2.10")
    _install(home, ".codex", "1.10.0")
    _install(home, ".codex", "1.2.9")
    node = _node(home / ".volta" / "bin", "v22.1.0")
    # The newest launcher runs: an older one replaced by a stub would fail.
    for v in ("1.2.9", "1.2.10"):
        (home / ".codex/plugins/cache/loom/loom" / v / "bin/loom-node").write_text("exit 1\n")
    assert _checks(home)["client:codex:node"] == (
        dc.PASS,
        f"Loom hook runtime ready: {node} (v22.1.0)",
    )


def test_a_version_manager_node_passes_without_the_shell_path(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    _install(home, ".codex", "1.3.0")
    node = _node(home / ".nvm" / "versions" / "node" / "v22.1.0" / "bin", "v22.1.0")
    line = f"Loom hook runtime ready: {node} (v22.1.0)"
    assert _checks(home) == {
        "client:claude-code:node": (dc.PASS, line),
        "client:codex:node": (dc.PASS, line),
    }


def test_a_node_only_on_the_shell_path_warns_for_claude_code(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    node = _node(home / "shell-only", "v24.0.0")
    grade, line = _checks(home, PATH=str(node.parent))["client:claude-code:node"]
    assert grade == dc.WARN
    assert line.startswith(f"Loom hook runtime ready: {node} (v24.0.0), but only with this shell")
    assert "LOOM_NODE to that path in the `env` block of ~/.claude/settings.json" in line


def test_shell_only_variables_do_not_reach_the_gui_run(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    node = _node(home / "custom-nvm" / "versions" / "node" / "v22.1.0" / "bin", "v22.1.0")
    assert _checks(home, LOOM_NODE=str(node))["client:claude-code:node"][0] == dc.WARN
    assert _checks(home, NVM_DIR=str(home / "custom-nvm"))["client:claude-code:node"][0] == dc.WARN


def test_loom_node_in_claude_settings_counts_for_claude_code_only(home: Path) -> None:
    _install(home, ".claude", "1.2.7")
    _install(home, ".codex", "1.2.8")
    node = _node(home / "elsewhere", "v24.0.0")
    _settings(home, {"LOOM_NODE": str(node)})
    checks = _checks(home)
    assert checks["client:claude-code:node"][0] == dc.PASS
    grade, line = checks["client:codex:node"]
    assert grade == dc.FAIL
    assert line.startswith("loom: Node.js 18+ not found")


def test_loom_node_reaches_codex_hooks_but_not_its_header_helper(home: Path) -> None:
    _install(home, ".codex", "1.3.0")
    node = _node(home / "elsewhere", "v24.0.0")
    grade, line = _checks(home, LOOM_NODE=str(node))["client:codex:node"]
    assert grade == dc.WARN
    assert "the MCP header helper, which Codex runs without LOOM_NODE" in line


def test_no_node_fails_with_the_launcher_line(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    old = _node(home / ".volta" / "bin", "v16.20.2")
    grade, line = _checks(home)["client:claude-code:node"]
    assert grade == dc.FAIL
    assert line.startswith(
        f"loom: Node.js 18+ is required; the first Node found, {old} (v16.20.2), is older."
    )
    assert "the MCP entry gets no token" in line and line.endswith("Fix: /loom:loom-status.")


def test_no_sh_fails_both_clients(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(home, ".claude", "1.3.0")
    _install(home, ".codex", "1.3.0")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _checks(home) == {
        "client:claude-code:node": (dc.FAIL, dc._NO_SH),
        "client:codex:node": (dc.FAIL, dc._NO_SH),
    }


def test_on_windows_codex_is_skipped_and_claude_code_has_no_gui_run(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(home, ".claude", "1.3.0")
    _install(home, ".codex", "1.3.0")
    node = _node(home / "shell-only", "v24.0.0")
    _settings(home, {"CLAUDE_CODE_GIT_BASH_PATH": "/bin/sh"})
    monkeypatch.setattr(dc, "WINDOWS", True)
    assert _checks(home, PATH=str(node.parent)) == {
        "client:claude-code:node": (dc.PASS, f"Loom hook runtime ready: {node} (v24.0.0)")
    }


def test_a_launcher_that_does_not_run_is_a_warning_not_a_verdict(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _install(home, ".claude", "1.3.0")

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", refuse)
    assert _checks(home)["client:claude-code:node"] == (
        dc.WARN,
        f"could not run {launcher}: [Errno 13] Permission denied",
    )


def test_settings_env_keeps_only_strings(home: Path) -> None:
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"LOOM_HOOK_TIMEOUT_MS": None, "LOOM_NODE": "/x", "N": 0}})
    )
    assert dc._claude_settings_env(home) == {"LOOM_NODE": "/x"}
    assert dc.hook_override_check(home, {}) == []


def test_a_cache_left_after_uninstalling_or_disabling_gets_no_line(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    _install(home, ".codex", "1.3.0")
    plugins = home / ".claude" / "plugins"
    # The marketplace stays registered after `claude plugin uninstall`.
    (plugins / "known_marketplaces.json").write_text(
        json.dumps({"loom": {"repo": "ClickHouse/loom"}})
    )
    (plugins / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {}}))
    (home / ".codex" / "config.toml").write_text('[plugins."loom@loom"]\nenabled = false\n')
    assert _checks(home) == {}


def test_a_disabled_claude_code_plugin_gets_no_line(home: Path) -> None:
    _install(home, ".claude", "1.3.0")
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"loom@loom": False}})
    )
    assert _checks(home) == {}


def test_the_installed_version_is_checked_not_the_newest_cached(home: Path) -> None:
    record = home / ".claude" / "plugins" / "installed_plugins.json"
    installed = _install(home, ".claude", "1.2.7")
    kept = record.read_text()
    _install(home, ".claude", "9.9.9")  # a newer cache with a real launcher
    record.write_text(kept)
    assert dc.claude_plugin_install(home) == installed.parent.parent


def test_only_the_user_scope_install_is_checked(home: Path) -> None:
    cache = home / ".claude" / "plugins" / "cache" / "loom" / "loom"
    installs = [
        {
            "scope": "project",
            "projectPath": str(home / "repo"),
            "installPath": str(cache / "1.2.5"),
        },
        {"scope": "user", "installPath": str(cache / "1.2.6")},
    ]
    record = home / ".claude" / "plugins" / "installed_plugins.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"version": 2, "plugins": {"loom@loom": installs}}))
    assert dc.claude_plugin_install(home) == cache / "1.2.6"
    record.write_text(json.dumps({"version": 2, "plugins": {"loom@loom": installs[:1]}}))
    assert dc.claude_plugin_install(home) is None


@pytest.mark.parametrize("installs", [[{"scope": "user"}], {"scope": "user"}, [], "loom"], ids=str)
def test_a_malformed_install_record_gets_no_line(home: Path, installs: object) -> None:
    record = home / ".claude" / "plugins" / "installed_plugins.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"plugins": {"loom@loom": installs}}))
    assert _checks(home) == {}


def test_a_plugin_from_before_the_launcher_is_named(home: Path) -> None:
    claude = _install(home, ".claude", "1.2.6")
    codex = _install(home, ".codex", "1.2.6")
    claude.unlink()
    codex.unlink()
    assert _checks(home) == {
        "client:claude-code:node": (
            dc.WARN,
            f"the installed plugin at {claude.parent.parent} predates the Node launcher: its "
            "hooks need `node` on Claude Code's PATH. Fix: `claude plugin update loom@loom`.",
        ),
        "client:codex:node": (
            dc.WARN,
            f"the installed plugin at {codex.parent.parent} predates the Node launcher: its "
            "hooks need `node` on Codex's PATH. Fix: `codex plugin marketplace upgrade loom`.",
        ),
    }


def test_on_windows_git_bash_is_found_as_claude_code_finds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc, "WINDOWS", True)
    git = tmp_path / "Git" / "cmd" / "git.exe"
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    for p in (git, bash):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    monkeypatch.setattr(shutil, "which", lambda name: str(git) if name == "git" else None)
    assert dc.posix_sh({}) == str(bash)
    assert dc.posix_sh({"CLAUDE_CODE_GIT_BASH_PATH": str(bash)}) == str(bash)
    # A CLAUDE_CODE_GIT_BASH_PATH that is gone is no sh either.
    assert dc.posix_sh({"CLAUDE_CODE_GIT_BASH_PATH": str(tmp_path / "gone.exe")}) is None
    # Another sh on PATH is not what Claude Code runs hooks with.
    monkeypatch.setattr(shutil, "which", lambda name: "C:/msys64/usr/bin/sh.exe")
    assert dc.posix_sh({}) is None


def test_on_windows_git_bash_from_claude_settings_counts(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(home, ".claude", "1.3.0")
    _node(home / ".volta" / "bin", "v22.1.0")
    monkeypatch.setattr(dc, "WINDOWS", True)
    _settings(home, {"CLAUDE_CODE_GIT_BASH_PATH": "/bin/sh"})
    assert _checks(home)["client:claude-code:node"][0] == dc.PASS


def test_an_install_that_is_not_on_disk_is_named(home: Path) -> None:
    launcher = _install(home, ".claude", "1.2.7")
    shutil.rmtree(launcher.parent.parent)
    assert _checks(home)["client:claude-code:node"] == (
        dc.WARN,
        f"the installed plugin at {launcher.parent.parent} is not on disk. Fix: "
        "`claude plugin uninstall loom@loom && claude plugin install loom@loom`.",
    )


@pytest.mark.parametrize("settings", ["{not json", "[]", '{"enabledPlugins": []}'])
def test_unreadable_settings_do_not_hide_the_row(home: Path, settings: str) -> None:
    _install(home, ".claude", "1.3.0")
    node = _node(home / ".volta" / "bin", "v22.1.0")
    (home / ".claude" / "settings.json").write_text(settings)
    # Read as no settings: the row is there, and Volta under HOME passes both runs.
    assert _checks(home)["client:claude-code:node"] == (
        dc.PASS,
        f"Loom hook runtime ready: {node} (v22.1.0)",
    )


def test_a_pre_launcher_codex_plugin_is_named_even_without_sh(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(home, ".codex", "1.2.6").unlink()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    grade, line = _checks(home)["client:codex:node"]
    assert grade == dc.WARN and "codex plugin marketplace upgrade loom" in line
