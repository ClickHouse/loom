"""The Codex plugin is the Claude Code plugin with different
wiring. The copies are current, the wiring names only scripts and events that
exist, both manifests agree, the marketplace points at the plugin, and
`loom install --client codex` registers through `codex mcp add`."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLAUDE = ROOT / "integrations" / "claude-code-plugin"
CODEX = ROOT / "integrations" / "codex-plugin"

# Codex hook events (learn.chatgpt.com/docs/hooks, 2026-09).
CODEX_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "UserPromptSubmit",
    "Stop",
    "Interrupt",
}


def _hooks(path: Path) -> dict:
    return json.loads(path.read_text())["hooks"]


def _commands(hooks: dict) -> list[tuple[str, str]]:
    out = []
    for event, groups in hooks.items():
        for g in groups:
            for h in g["hooks"]:
                out.append((event, h["command"]))
    return out


def test_codex_copies_are_current() -> None:
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "codegen" / "agent_plugin_sync.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr


def test_codex_wiring_names_codex_events_and_existing_scripts() -> None:
    hooks = _hooks(CODEX / "hooks" / "hooks.json")
    assert set(hooks) <= CODEX_EVENTS, set(hooks) - CODEX_EVENTS
    for event, cmd in _commands(hooks):
        m = re.fullmatch(
            r'sh "\$\{PLUGIN_ROOT\}/bin/loom-node" "\$\{PLUGIN_ROOT\}/hooks/([a-z-]+\.mjs)" (\w+) codex',
            cmd,
        )
        assert m, f"{event}: unexpected command shape: {cmd}"
        script, argv_event = m.group(1), m.group(2)
        assert (CODEX / "hooks" / script).is_file(), f"{event}: {script} missing"
        assert argv_event == event, f"{event}: script told {argv_event}"


def test_codex_wiring_matches_on_codex_tool_names() -> None:
    hooks = _hooks(CODEX / "hooks" / "hooks.json")
    pre = [g.get("matcher", "") for g in hooks["PreToolUse"]]
    post = [g.get("matcher", "") for g in hooks["PostToolUse"]]
    # the shunt watches the shell tool (Codex has no Read/Grep), the edit
    # hooks watch apply_patch
    assert any("Bash" in m and "shell" in m for m in pre)
    assert any("apply_patch" in m for m in pre)
    assert any("apply_patch" in m for m in post)
    assert not any(m in {"Read", "Grep", "Read|Grep|Bash"} for m in pre)


def test_codex_session_end_runs_only_fire_and_forget_scripts() -> None:
    """Codex gives SessionEnd hooks one second; the conversation hook (which
    waits for a settle and reads a file) does its work at Stop."""
    hooks = _hooks(CODEX / "hooks" / "hooks.json")
    end = {
        re.search(r"hooks/([a-z-]+)\.mjs", c).group(1)
        for _, c in _commands({"SessionEnd": hooks["SessionEnd"]})
    }
    assert "loom-conversation" not in end
    assert "loom-usage" not in {
        re.search(r"hooks/([a-z-]+)\.mjs", c).group(1) for _, c in _commands(hooks)
    }
    assert {"loom-capture", "loom-run", "loom-session-memory"} <= end


def test_every_claude_hook_script_is_wired_or_named_as_left_out() -> None:
    """A script the Claude Code plugin runs is either wired for Codex or the
    Codex hooks.json says why it is not (loom-usage)."""
    claude_scripts = {p.stem for p in (CLAUDE / "hooks").glob("loom-*.mjs")} - {"loom-flush"}
    codex_hooks = (CODEX / "hooks" / "hooks.json").read_text()
    wired = set(re.findall(r"hooks/(loom-[a-z-]+)\.mjs\" \w+ codex", codex_hooks))
    left_out = claude_scripts - wired
    for name in left_out:
        assert name in codex_hooks, (
            f"{name} is neither wired for Codex nor explained in its hooks.json"
        )


def test_manifests_agree_and_marketplace_points_at_the_plugin() -> None:
    codex = json.loads((CODEX / ".codex-plugin" / "plugin.json").read_text())
    claude = json.loads((CLAUDE / ".claude-plugin" / "plugin.json").read_text())
    assert codex["name"] == claude["name"] == "loom"
    assert codex["version"] == claude["version"], "one plugin version, stated twice"
    assert codex["skills"] == "./skills/"
    assert {p.name for p in (CODEX / "skills").iterdir()} == {
        p.name for p in (CLAUDE / "skills").iterdir()
    }
    mp = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
    entry = next(p for p in mp["plugins"] if p["name"] == "loom")
    assert entry["source"] == {"source": "local", "path": "./integrations/codex-plugin"}
    assert (ROOT / entry["source"]["path"] / ".codex-plugin" / "plugin.json").is_file()


def test_claude_hooks_json_still_passes_no_client_argument() -> None:
    """Claude Code stays the default client: its wiring passes no client
    argument, so the scripts fall back to `claude-code`."""
    for _, cmd in _commands(_hooks(CLAUDE / "hooks" / "hooks.json")):
        assert re.fullmatch(
            r'sh "\$\{CLAUDE_PLUGIN_ROOT\}/bin/loom-node" "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/[a-z-]+\.mjs" \w+',
            cmd,
        ), cmd


# ---- loom install --client codex -------------------------------------------


def test_install_codex_registers_through_codex_mcp_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.cli.install as cli

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert cli._INSTALL_CLIENTS["codex"]["linux"].endswith("config.toml")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli._install_one(
            "codex", token="T.T.T", server_url="https://h/", name="loom", dry_run=True
        )
    out = buf.getvalue()
    assert (
        'codex mcp add loom "https://h/mcp?toolset=coding"' not in out
    )  # url is a flag, not positional
    assert (
        'codex mcp add loom --url "https://h/mcp?toolset=coding" --bearer-token-env-var LOOM_API_KEY'
        in out
    )
    assert "[mcp_servers.loom]" in out and 'bearer_token_env_var = "LOOM_API_KEY"' in out
    assert "export LOOM_API_KEY=T.T.T" in out
    # the token never lands in the TOML Codex keeps
    toml = out.split("[mcp_servers.loom]", 1)[1].split("Codex reads", 1)[0]
    assert "T.T.T" not in toml


def test_install_codex_dry_run_runs_nothing_and_capture_writes_loom_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.cli.install as cli

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".loom" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"namespace": "keep-me"}))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli._install_codex_capture("https://h/", "CAP", dry_run=False)
    d = json.loads(cfg.read_text())
    assert d == {"namespace": "keep-me", "url": "https://h", "token": "CAP"}
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    out = buf.getvalue()
    assert (
        "codex plugin marketplace add ClickHouse/loom" in out
        and "codex plugin add loom@loom" in out
    )


def test_setup_endpoint_client_table_names_codex() -> None:
    from server.identity.app.me_setup import MCP_SETUP_CLIENT_LABELS

    assert MCP_SETUP_CLIENT_LABELS["codex"] == "Codex"
    # the dashboard's picker mirrors this table
    ts = (ROOT / "frontend" / "src" / "lib" / "clients.ts").read_text()
    for cid in MCP_SETUP_CLIENT_LABELS:
        assert f'"{cid}"' in ts, f"frontend client list lacks {cid}"


def test_codex_plugin_bundles_its_mcp_entry() -> None:
    """The Codex plugin carries its own MCP entry, so install
    is `codex plugin add` alone — no `codex mcp add`, no LOOM_API_KEY."""
    manifest = json.loads((CODEX / ".codex-plugin" / "plugin.json").read_text())
    assert manifest["mcpServers"] == "./.mcp.json"
    servers = json.loads((CODEX / ".mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"loom"}
    entry = servers["loom"]
    assert entry["type"] == "http"
    defaults = json.loads((CODEX / "plugin-defaults.json").read_text())
    assert entry["url"] == f"{defaults['server_url']}/mcp?toolset=core,coding,tracker,tasks"
    # Codex runs the helper through `sh -c` in the session's cwd with a cleared
    # environment (HOME kept, no PLUGIN_ROOT/CODEX_HOME), so the command finds
    # the newest installed copy of this plugin's own launcher under $HOME and
    # runs the helper beside it.
    helper = entry["http_headers_helper"]
    assert '"$HOME"/.codex/plugins/cache/loom/loom/*/bin/loom-node' in helper
    assert '"${l%/*}/loom-mcp-headers.mjs"' in helper
    assert "sort -V | tail -1" in helper
    assert "bearer_token_env_var" not in entry  # the token comes from ~/.loom/config.json
    assert (CODEX / "bin" / "loom-mcp-headers.mjs").is_file()
    assert (CODEX / "bin" / "loom-node").is_file()


def test_both_plugins_ship_one_version() -> None:
    codex = json.loads((CODEX / ".codex-plugin" / "plugin.json").read_text())["version"]
    claude = json.loads(
        (
            ROOT / "integrations" / "claude-code-plugin" / ".claude-plugin" / "plugin.json"
        ).read_text()
    )["version"]
    assert codex == claude


def test_listing_icons_are_files_the_plugins_ship() -> None:
    """The directory listings read these paths; one that does not exist ships a listing
    with no icon."""
    claude = json.loads((CLAUDE / ".claude-plugin" / "plugin.json").read_text())
    skill = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    interface = json.loads((CODEX / ".codex-plugin" / "plugin.json").read_text())["interface"]
    assert (CLAUDE / claude["icon"]).is_file()
    assert (ROOT / skill["icon"]).is_file()
    assert (CODEX / interface["logo"]).is_file()
    assert (CODEX / interface["composerIcon"]).is_file()


def test_codex_default_prompts_fit_the_directory() -> None:
    """Codex shows at most three default prompts of at most 128 characters each."""
    prompts = json.loads((CODEX / ".codex-plugin" / "plugin.json").read_text())["interface"][
        "defaultPrompt"
    ]
    assert isinstance(prompts, list)
    assert 1 <= len(prompts) <= 3
    assert all(len(p) <= 128 for p in prompts)


def test_both_plugins_link_the_data_handling_section() -> None:
    """The privacy link of both listings is an absolute URL, which the doc-link check does
    not follow; renaming the section would leave both listings pointing at nothing."""
    claude = json.loads((CLAUDE / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((CODEX / ".codex-plugin" / "plugin.json").read_text())
    url = claude["privacyPolicyUrl"]
    assert url == codex["interface"]["privacyPolicyURL"]
    page, _, anchor = url.removeprefix("https://github.com/ClickHouse/loom/blob/main/").partition(
        "#"
    )
    assert anchor == "data-handling" and "\n## Data handling\n" in (ROOT / page).read_text()


def test_listing_links_into_the_repository_resolve() -> None:
    """The registry listing and both plugin manifests publish absolute links into this
    repository, which the doc-link check does not follow; a renamed file breaks them."""
    text = "".join(
        (ROOT / f).read_text()
        for f in (
            "server.json",
            "integrations/claude-code-plugin/.claude-plugin/plugin.json",
            "integrations/codex-plugin/.codex-plugin/plugin.json",
        )
    )
    links = re.findall(
        r"https://(?:github\.com/ClickHouse/loom/blob/main|raw\.githubusercontent\.com/ClickHouse/loom/main)/([^\"#]+)",
        text,
    )
    assert links
    for path in links:
        assert (ROOT / path).is_file(), path
