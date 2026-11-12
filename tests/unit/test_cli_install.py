"""`loom install` — per-client config merge, client detection, and the Claude
Code capture (settings.json `env`) writer."""

from __future__ import annotations

import json

import pytest

import server.cli.install as cli
from server.identity.app.token_clients import CODING_CLIENTS, MCP_CLIENTS, client_config


@pytest.fixture(autouse=True)
def _no_plugin_home(tmp_path_factory, monkeypatch: pytest.MonkeyPatch):
    """The claude-code installer reads ~/.claude for the
    loom plugin; these tests run in an empty home so a developer machine with
    the plugin installed sees the same answers CI does."""
    from pathlib import Path

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_install_one_url_form_preserves_keys(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "cursor" / "mcp.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"other": 1, "mcpServers": {"keep": {"url": "x"}}}))

    assert cli._install_one("cursor", token="T", server_url="https://h", name="loom", dry_run=False)
    d = json.loads(cfg.read_text())
    # Cursor is a coding agent: the coding listing, as the dashboard gives it
    assert d["mcpServers"]["loom"]["url"] == "https://h/mcp?toolset=coding"
    assert d["mcpServers"]["loom"]["headers"]["Authorization"] == "Bearer T"
    assert d["other"] == 1 and "keep" in d["mcpServers"]  # unrelated keys preserved


def test_install_one_lists_the_server_default_for_a_chat_client(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "windsurf" / "mcp_config.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    cfg.parent.mkdir(parents=True)

    assert cli._install_one(
        "windsurf", token="T", server_url="https://h", name="loom", dry_run=False
    )
    assert json.loads(cfg.read_text())["mcpServers"]["loom"]["url"] == "https://h/mcp"


def test_install_one_npx_form_for_stdio_clients(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "cd" / "c.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    assert cli._install_one(
        "claude-desktop", token="T", server_url="https://h", name="loom", dry_run=False
    )
    entry = json.loads(cfg.read_text())["mcpServers"]["loom"]
    assert entry["command"] == "npx" and "mcp-remote" in entry["args"]


def test_install_one_rejects_invalid_json_without_clobbering(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cursor" / "mcp.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ not json")
    assert (
        cli._install_one("cursor", token="T", server_url="https://h", name="loom", dry_run=False)
        is False
    )
    assert cfg.read_text() == "{ not json"  # left untouched on parse failure


def test_detect_installed_clients(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = {c: tmp_path / c / "cfg.json" for c in cli._INSTALL_CLIENTS}
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: paths[c])
    (tmp_path / "cursor").mkdir(parents=True)
    (tmp_path / "zed").mkdir(parents=True)
    assert set(cli._detect_installed_clients()) == {"cursor", "zed"}


def test_claude_code_capture_writes_env_preserving_settings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    scfg = tmp_path / ".claude" / "settings.json"
    scfg.parent.mkdir(parents=True)
    scfg.write_text(json.dumps({"theme": "dark", "env": {"KEEP": "1"}}))

    assert cli._install_claude_code_capture("https://hosted.example/", "CAP", dry_run=False)
    d = json.loads(scfg.read_text())
    assert d["env"] == {
        "KEEP": "1",
        "LOOM_BASE_URL": "https://hosted.example",
        "LOOM_API_KEY": "CAP",
    }
    assert d["theme"] == "dark"  # unrelated settings preserved


def test_install_one_toolset_rides_the_url(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--toolset coding` selects the coding listing per request
    (`?toolset=` on the MCP URL) so one deployment serves both archetypes; a
    coding agent such as Cursor gets `coding` by default, and a named toolset wins."""
    cfg = tmp_path / "cursor" / "mcp.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    assert cli._install_one(
        "cursor",
        token="T",
        server_url="https://h/",
        name="loom",
        dry_run=False,
        toolset="coding",
    )
    d = json.loads(cfg.read_text())
    assert d["mcpServers"]["loom"]["url"] == "https://h/mcp?toolset=coding"
    assert cli._install_one(
        "cursor", token="T", server_url="https://h", name="loom", dry_run=False, toolset="tracker"
    )
    assert (
        json.loads(cfg.read_text())["mcpServers"]["loom"]["url"] == "https://h/mcp?toolset=tracker"
    )
    # the stdio bridge form carries the same URL
    cfg2 = tmp_path / "cd" / "c.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg2)
    assert cli._install_one(
        "claude-desktop",
        token="T",
        server_url="https://h",
        name="loom",
        dry_run=False,
        toolset="coding",
    )
    assert (
        "https://h/mcp?toolset=coding" in json.loads(cfg2.read_text())["mcpServers"]["loom"]["args"]
    )


def test_plugin_mcp_json_mounts_the_coding_toolset() -> None:
    """The coding-agent plugin's own MCP block: the coding
       toolset on the URL, a localhost default when LOOM_BASE_URL is unset, and the
       bearer from a `headersHelper` that reads the same places the hooks do
    — no export is required."""
    from pathlib import Path

    root = Path(cli.__file__).resolve().parents[2]
    block = json.loads((root / "integrations/claude-code-plugin/.mcp.json").read_text())
    loom = block["mcpServers"]["loom"]
    # The URL is the install-time option, whose default keeps
    # the LOOM_BASE_URL-then-localhost behaviour (Claude Code expands the env form
    # inside a substituted default — verified on 2.1.273)
    # The toolset is the plugin's `toolset` setting (default `coding`)
    # The fragment hands the header helper the project directory (never sent)
    assert (
        loom["url"]
        == "${user_config.server_url}/mcp?toolset=${user_config.toolset}#${CLAUDE_PROJECT_DIR}"
    )
    manifest = json.loads(
        (root / "integrations/claude-code-plugin/.claude-plugin/plugin.json").read_text()
    )
    assert manifest["userConfig"]["toolset"]["default"] == "coding"
    assert "headers" not in loom
    assert "bin/loom-mcp-headers.mjs" in loom["headersHelper"]
    assert (root / "integrations/claude-code-plugin/bin/loom-mcp-headers.mjs").is_file()
    assert loom["type"] == "http"


# ---- Claude Code: `claude mcp add`, never ~/.claude/mcp.json ----

# The header names the variable (single-quoted so the shell leaves it for
# Claude Code to expand); the token never appears on argv or in output.
_CC_CMD = (
    'claude mcp add --transport http loom "https://h/mcp?toolset=coding" '
    "-H 'Authorization: Bearer ${LOOM_API_KEY}' -s user"
)


def _no_claude_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)


def test_claude_code_install_target_is_not_claude_mcp_json(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Claude Code has zero references to ~/.claude/mcp.json (it reads
    ~/.claude.json and a project .mcp.json); the install must not create it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _no_claude_binary(monkeypatch)
    for key in ("darwin", "linux", "win32"):
        assert not cli._INSTALL_CLIENTS["claude-code"][key].endswith("mcp.json")
    assert cli._install_one(
        "claude-code", token="T", server_url="https://h", name="loom", dry_run=False
    )
    assert not (tmp_path / ".claude" / "mcp.json").exists()
    assert not (tmp_path / ".claude.json").exists()  # only `claude mcp add` writes it
    out = capsys.readouterr().out
    assert _CC_CMD in out


def test_claude_code_install_prints_command_and_project_block_when_claude_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _no_claude_binary(monkeypatch)
    assert cli._install_claude_code_mcp(
        server_url="https://h/", name="loom", dry_run=False, toolset="coding"
    )
    out = capsys.readouterr().out
    assert "not on PATH" in out
    assert _CC_CMD in out
    # the project-scoped alternative, in the shape Claude Code reads
    start = out.index("{\n")
    block = json.loads(out[start : out.rindex("}") + 1].replace("\n    ", "\n"))
    assert "reads the token from $LOOM_API_KEY" in out
    assert block == {
        "mcpServers": {
            "loom": {
                "type": "http",
                "url": "https://h/mcp?toolset=coding",
                "headers": {"Authorization": "Bearer ${LOOM_API_KEY}"},
            }
        }
    }


def test_claude_code_install_runs_claude_mcp_add_when_on_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the binary present the install is the same one-shot the other
    clients get (no prompt, no flag): `claude mcp add … -s user` runs."""
    import shutil
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
    calls: list[list[str]] = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or _Done())
    assert cli._install_one(
        "claude-code", token="T", server_url="https://h", name="loom", dry_run=False
    )
    assert calls == [
        [
            "claude",
            "mcp",
            "add",
            "--transport",
            "http",
            "loom",
            "https://h/mcp?toolset=coding",
            "-H",
            "Authorization: Bearer ${LOOM_API_KEY}",
            "-s",
            "user",
        ]
    ]
    assert "registered loom via `claude mcp add`" in capsys.readouterr().out


def test_claude_code_install_reports_claude_mcp_add_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")

    class _Failed:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _Failed())
    assert (
        cli._install_claude_code_mcp(
            server_url="https://h", name="loom", dry_run=False, toolset="coding"
        )
        is False
    )
    err = capsys.readouterr().err
    assert "exited 1" in err and _CC_CMD in err


def test_claude_code_install_dry_run_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")

    def _boom(*a, **kw):  # pragma: no cover — the assertion is that it never runs
        raise AssertionError("claude mcp add must not run under --dry-run")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert cli._install_claude_code_mcp(
        server_url="https://h", name="loom", dry_run=True, toolset="coding"
    )
    out = capsys.readouterr().out
    assert out.startswith("would run: " + _CC_CMD)


def test_claude_code_toolset_flag_overrides_the_coding_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Claude Code is a coding agent, so the URL carries `?toolset=coding`
    unless --toolset names another tier."""
    _no_claude_binary(monkeypatch)
    cli._install_claude_code_mcp(server_url="https://h", name="mem", dry_run=True, toolset="core")
    out = capsys.readouterr().out
    assert 'mem "https://h/mcp?toolset=core"' in out
    assert "toolset=coding" not in out


def test_shell_words_quotes_only_what_needs_it() -> None:
    assert cli._shell_words(["claude", "mcp", "add", "-s", "user"]) == "claude mcp add -s user"
    assert cli._shell_words(["a b", 'say "hi"']) == '"a b" "say \\"hi\\""'
    assert cli._shell_words(["https://h/mcp?toolset=coding"]) == '"https://h/mcp?toolset=coding"'


def test_claude_code_install_skips_the_user_scope_entry_when_the_plugin_mounts_it(
    _no_plugin_home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plugin's `.mcp.json` already mounts the server; a
    user-scope entry on top lists every Loom tool twice. --force still adds it."""
    import shutil
    import subprocess

    (_no_plugin_home / ".claude" / "plugins" / "data" / "loom").mkdir(parents=True)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
    ran: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, check=False: ran.append(argv) or type("R", (), {"returncode": 0})(),
    )
    assert cli._install_claude_code_mcp(
        server_url="https://h", name="loom", dry_run=False, toolset=""
    )
    assert ran == [] and "mounts the server itself" in capsys.readouterr().out
    assert cli._install_claude_code_mcp(
        server_url="https://h", name="loom", dry_run=False, toolset="", force=True
    )
    assert ran and ran[0][:3] == ["claude", "mcp", "add"]


def test_claude_code_install_never_puts_the_token_on_argv_or_in_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`claude mcp add`'s argv is visible in `ps` and its failure line goes to
    stderr: the header names ${LOOM_API_KEY} (Claude Code expands it at
    launch from settings.json `env`, which the capture step fills)."""
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
    ran: list[list[str]] = []

    class _Failed:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv) or _Failed())
    assert not cli._install_one(
        "claude-code", token="SECRET-TOKEN", server_url="https://h", name="loom", dry_run=False
    )
    captured = capsys.readouterr()
    assert ran and not any("SECRET-TOKEN" in a for a in ran[0])
    assert "SECRET-TOKEN" not in captured.out + captured.err


def test_install_reads_the_saved_token_and_the_shared_base_url(
    _no_plugin_home, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like every other client command: --token, then LOOM_API_KEY, then
    ~/.loom/token; --server-url defaults to LOOM_BASE_URL."""
    import argparse

    monkeypatch.delenv("LOOM_API_KEY", raising=False)
    (_no_plugin_home / ".loom").mkdir()
    (_no_plugin_home / ".loom" / "token").write_text("SAVED\n")
    monkeypatch.setenv("LOOM_BASE_URL", "https://hosted.example")
    p = argparse.ArgumentParser()
    cli._add_install_flags(p)
    args = p.parse_args(["--client", "cursor"])
    assert args.server_url == "https://hosted.example"
    cfg = tmp_path / "cursor" / "mcp.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    assert cli._cmd_install(args) == 0
    entry = json.loads(cfg.read_text())["mcpServers"]["loom"]
    assert entry == {
        "url": "https://hosted.example/mcp?toolset=coding",
        "headers": {"Authorization": "Bearer SAVED"},
    }


def test_dry_run_previews_only_the_keys_it_sets_and_masks_the_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run printed the whole merged file: this token plus every other
    credential the file already held. It prints only what it would set."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "cursor" / "mcp.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    cfg.parent.mkdir(parents=True)
    other = {"mcpServers": {"other": {"headers": {"Authorization": "Bearer OTHER-SECRET"}}}}
    cfg.write_text(json.dumps(other))
    scfg = tmp_path / ".claude" / "settings.json"
    scfg.parent.mkdir(parents=True)
    scfg.write_text(json.dumps({"env": {"OPENAI_API_KEY": "sk-ENV-SECRET"}}))
    lcfg = tmp_path / ".loom" / "config.json"
    lcfg.parent.mkdir(parents=True)
    lcfg.write_text(json.dumps({"token": "OLD-SECRET", "note": "kept"}))

    tok = "NEW.TOKEN.VALUE"
    assert cli._install_one("cursor", token=tok, server_url="https://h", name="loom", dry_run=True)
    assert cli._install_claude_code_capture("https://h", tok, dry_run=True)
    assert cli._install_codex_capture("https://h", tok, dry_run=True)

    out = capsys.readouterr().out
    for secret in (tok, "OTHER-SECRET", "sk-ENV-SECRET", "OLD-SECRET"):
        assert secret not in out
    assert out.count("<token>") == 3
    assert '"url": "https://h/mcp?toolset=coding"' in out and '"LOOM_BASE_URL": "https://h"' in out
    # nothing was written
    assert json.loads(cfg.read_text()) == other
    assert json.loads(lcfg.read_text()) == {"token": "OLD-SECRET", "note": "kept"}


@pytest.mark.parametrize("client", sorted(set(MCP_CLIENTS) - {"claude-code", "codex"}))
def test_install_gives_each_client_the_toolset_the_server_setup_gives(
    client: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty --toolset writes the URL the dashboard's and the token setup's
    entry for the client carries: `coding` for a coding agent, else bare."""
    cfg = tmp_path / client / "config.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    cfg.parent.mkdir(parents=True)
    assert cli._install_one(client, token="T", server_url="https://h", name="loom", dry_run=False)
    written = json.dumps(json.loads(cfg.read_text()))
    setup = json.dumps(client_config(client, "https://h/mcp", "T")["config"])
    expected = "https://h/mcp?toolset=coding" if client in CODING_CLIENTS else "https://h/mcp"
    assert expected in written and expected in setup
    assert "toolset=coding" not in written or client in CODING_CLIENTS
