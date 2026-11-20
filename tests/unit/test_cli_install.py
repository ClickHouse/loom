"""`loom install` — per-client config merge, client detection, and the Claude
Code capture (settings.json `env`) writer."""

from __future__ import annotations

import json
import sys

import pytest

import server.cli.install as cli
from server.cli.jsonc_edit import JsoncEditError, upsert_object_key
from server.identity.app.token_clients import MCP_CLIENTS, client_config


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
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    (tmp_path / "cursor").mkdir(parents=True)
    (tmp_path / "zed").mkdir(parents=True)
    assert set(cli._detect_installed_clients()) == {"cursor", "zed"}


def test_gemini_is_detected_by_its_settings_or_binary_not_by_the_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Google Antigravity also creates ~/.gemini/, so the directory alone is not Gemini CLI.
    paths = {c: tmp_path / c / "cfg.json" for c in cli._INSTALL_CLIENTS}
    paths["gemini"] = tmp_path / ".gemini" / "settings.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: paths[c])
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    (tmp_path / ".gemini" / "antigravity").mkdir(parents=True)
    assert "gemini" not in cli._detect_installed_clients()

    paths["gemini"].write_text("{}")
    assert "gemini" in cli._detect_installed_clients()

    paths["gemini"].unlink()
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/local/bin/gemini" if name == "gemini" else None
    )
    assert "gemini" in cli._detect_installed_clients()


def test_install_one_gemini_writes_http_url_and_keeps_other_settings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / ".gemini" / "settings.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    cfg.parent.mkdir(parents=True)
    other = {"command": "node", "args": ["server.js"]}
    cfg.write_text(json.dumps({"ui": {"theme": "Dracula"}, "mcpServers": {"other": other}}))

    for token in ("T1", "T2"):  # a first write, then a rewrite with a new token
        assert cli._install_one(
            "gemini", token=token, server_url="https://h", name="loom", dry_run=False
        )
        d = json.loads(cfg.read_text())
        # Gemini CLI reads streamable HTTP from httpUrl, and is a coding agent
        assert d["mcpServers"]["loom"] == {
            "httpUrl": "https://h/mcp?toolset=coding",
            "headers": {"Authorization": f"Bearer {token}"},
        }
        assert d["ui"] == {"theme": "Dracula"} and d["mcpServers"]["other"] == other


def test_install_one_refuses_a_settings_file_with_comments_and_prints_the_entry(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / ".gemini" / "settings.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    cfg.parent.mkdir(parents=True)
    text = '{\n  // my theme\n  "ui": {"theme": "Dracula"} /* end */\n}\n'
    cfg.write_text(text)

    ok = cli._install_one(
        "gemini", token="SECRET", server_url="https://h", name="loom", dry_run=False
    )

    assert ok is False
    assert cfg.read_text() == text  # nothing written, comments kept
    err = capsys.readouterr().err
    assert str(cfg) in err and "comments" in err
    pasted = json.loads(err[err.index("{") :])
    assert pasted == {
        "loom": {
            "httpUrl": "https://h/mcp?toolset=coding",
            "headers": {"Authorization": "Bearer <token>"},
        }
    }
    assert "SECRET" not in err


def test_has_json_comments_ignores_slashes_inside_strings() -> None:
    assert cli._has_json_comments('{"url": "https://h/mcp", "p": "a/*b*/c"}') is False
    assert cli._has_json_comments('{"a": "x\\"" // c\n}') is True
    assert cli._has_json_comments('{"a": 1 /* c */}') is True


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
    (`?toolset=` on the MCP URL) so one deployment serves both archetypes, and a
    named toolset wins over a client's default."""
    cfg = tmp_path / "mcp.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    assert cli._install_one(
        "windsurf",
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
    cli._install_one(
        "claude-code", token="T", server_url="https://h", name="mem", dry_run=True, toolset="core"
    )
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
        server_url="https://h", name="loom", dry_run=False, toolset="coding"
    )
    assert ran == [] and "mounts the server itself" in capsys.readouterr().out
    assert cli._install_claude_code_mcp(
        server_url="https://h", name="loom", dry_run=False, toolset="coding", force=True
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


@pytest.mark.parametrize(
    "tok", ["NEW.TOKEN.VALUE", 'secret"quote', "secret\\path", "secret\nline", "secret\U0001f512"]
)
def test_dry_run_previews_only_the_keys_it_sets_and_masks_the_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tok: str
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

    assert cli._install_one("cursor", token=tok, server_url="https://h", name="loom", dry_run=True)
    assert cli._install_claude_code_capture("https://h", tok, dry_run=True)
    assert cli._install_codex_capture("https://h", tok, dry_run=True)

    out = capsys.readouterr().out
    for secret in (tok, "OTHER-SECRET", "sk-ENV-SECRET", "OLD-SECRET"):
        assert secret not in out
        assert json.dumps(secret)[1:-1] not in out
    assert out.count("<token>") == 3
    assert '"url": "https://h/mcp?toolset=coding"' in out and '"LOOM_BASE_URL": "https://h"' in out
    # nothing was written
    assert json.loads(cfg.read_text()) == other
    assert json.loads(lcfg.read_text()) == {"token": "OLD-SECRET", "note": "kept"}


@pytest.mark.parametrize("dry_run", [False, True])
def test_codex_manual_setup_uses_a_token_placeholder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    token = 'secret"token\\value'
    assert cli._install_one(
        "codex", token=token, server_url="https://h", name="loom", dry_run=dry_run, toolset="coding"
    )
    out = capsys.readouterr().out
    assert token not in out
    assert cli._shell_words([token]) not in out
    assert 'export LOOM_API_KEY="<token>"' in out


@pytest.mark.parametrize("client", sorted(set(MCP_CLIENTS) - {"claude-code", "codex"}))
def test_install_gives_each_client_the_toolset_the_server_setup_gives(
    client: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty --toolset writes the URL the dashboard's and the token setup's
    entry for the client carries: `coding` for a coding agent, else bare."""
    cfg = tmp_path / client / "config.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda c: cfg)
    assert cli._install_one(client, token="T", server_url="https://h", name="loom", dry_run=False)
    setup = client_config(client, "https://h/mcp", "T")
    key = setup["config_key"]
    assert json.loads(cfg.read_text())[key] == setup["config"][key]


_ZED_SETTINGS = """\
// settings-file-marker: this comment identifies the settings file Zed should open

{
  "theme": "One Dark",
  "context_servers": {
    "marker-existing-server": {
      "command": "true",
      "args": ["existing-server-marker"]
    }
  }
}
"""

_ZED_TOKEN = "repro-not-a-secret"
_ZED_URL = "http://localhost:7777/mcp"


def _zed_path(tmp_path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "settings.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda _client: cfg)
    return cfg


def test_zed_config_path_is_the_settings_file() -> None:
    """Zed reads context_servers from settings.json. macOS uses the same
    ~/.config/zed path as Linux. Windows uses %APPDATA%\\Zed\\settings.json."""
    assert cli.CONFIG_PATHS["zed"] == {
        "darwin": "~/.config/zed/settings.json",
        "linux": "~/.config/zed/settings.json",
        "win32": "%APPDATA%\\Zed\\settings.json",
    }


def test_zed_resolve_install_path_follows_the_platform(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli._resolve_install_path("zed") == tmp_path / ".config" / "zed" / "settings.json"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli._resolve_install_path("zed") == tmp_path / ".config" / "zed" / "settings.json"


def test_zed_install_merges_jsonc_and_repeats_without_rewriting(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text(_ZED_SETTINGS)
    assert cli._install_one(
        "zed",
        token=_ZED_TOKEN,
        server_url="http://localhost:7777",
        name="loom",
        dry_run=False,
    )
    text = cfg.read_text()
    assert text.startswith(
        "// settings-file-marker: this comment identifies the settings file Zed should open\n"
    )
    assert '"theme": "One Dark"' in text
    assert '"marker-existing-server"' in text
    assert '"args": ["existing-server-marker"]' in text
    assert '"url": "http://localhost:7777/mcp"' in text
    assert f'"Authorization": "Bearer {_ZED_TOKEN}"' in text
    assert "mcp-remote" not in text
    assert "mcp.json" not in text
    again = text
    assert cli._install_one(
        "zed",
        token=_ZED_TOKEN,
        server_url="http://localhost:7777",
        name="loom",
        dry_run=False,
    )
    assert cfg.read_text() == again


def test_zed_install_keeps_trailing_commas_comments_and_url_slashes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = """\
{
  // keep this line
  "theme": "http://example.com/a//b", /* kept */
  "context_servers": {
    "marker-existing-server": {
      "command": "true",
      "args": ["keep-me",],
    },
  },
}
"""
    cfg.write_text(original)
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    text = cfg.read_text()
    assert "// keep this line" in text
    assert "/* kept */" in text
    assert '"theme": "http://example.com/a//b"' in text
    assert '"args": ["keep-me",]' in text
    assert text.rstrip().endswith("}")
    assert '"url": "http://localhost:7777/mcp"' in text
    assert "mcp-remote" not in text


def test_zed_install_replaces_only_the_loom_entry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text(_ZED_SETTINGS)
    assert cli._install_one(
        "zed", token="old-token", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    once = cfg.read_text()
    assert cli._install_one(
        "zed", token="new-token", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    text = cfg.read_text()
    assert "old-token" not in text
    assert "Bearer new-token" in text
    assert "// settings-file-marker" in text
    assert '"args": ["existing-server-marker"]' in text
    assert text != once


def test_zed_install_refuses_unmerged_jsonc_without_touching_bytes(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    broken = '{ "theme": "One Dark",\n'
    cfg.write_text(broken)
    assert (
        cli._install_one(
            "zed", token=_ZED_TOKEN, server_url="http://localhost:7777", name="loom", dry_run=False
        )
        is False
    )
    assert cfg.read_text() == broken
    err = capsys.readouterr().err
    assert "not changed" in err
    assert _ZED_URL in err
    assert _ZED_TOKEN not in err
    assert "Bearer <token>" in err
    assert "context_servers" in err


def test_zed_install_refuses_a_non_object_context_servers(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = '{ "theme": "One Dark", "context_servers": [] }\n'
    cfg.write_text(original)
    assert (
        cli._install_one(
            "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
        )
        is False
    )
    assert cfg.read_text() == original
    assert "context_servers" in capsys.readouterr().err


def test_zed_dry_run_names_settings_json_and_writes_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    settings = tmp_path / ".config" / "zed" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(_ZED_SETTINGS)
    before = settings.read_bytes()
    assert cli._install_one(
        "zed",
        token=_ZED_TOKEN,
        server_url="http://localhost:7777",
        name="loom",
        dry_run=True,
    )
    out = capsys.readouterr().out
    assert str(settings) in out
    assert "mcp.json" not in out
    assert _ZED_TOKEN not in out
    assert "<token>" in out
    assert settings.read_bytes() == before
    assert not (settings.parent / "mcp.json").exists()


def _jsonc_object(text: str) -> dict:
    """A JSONC object parsed for an assertion.

    Strings are copied through, so ``//`` inside a URL is not a comment. This
    is not the installer editor.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("//", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise AssertionError(f"unterminated comment\n{text}")
            i = end + 2
            continue
        out.append(text[i])
        i += 1
    stripped = "".join(out)
    cleaned: list[str] = []
    i = 0
    n = len(stripped)
    while i < n:
        if stripped[i] == '"':
            j = i + 1
            while j < n:
                if stripped[j] == "\\":
                    j += 2
                    continue
                if stripped[j] == '"':
                    j += 1
                    break
                j += 1
            cleaned.append(stripped[i:j])
            i = j
            continue
        if stripped[i] == ",":
            k = i + 1
            while k < n and stripped[k] in " \t\r\n":
                k += 1
            if k < n and stripped[k] in "}]":
                i += 1
                continue
        cleaned.append(stripped[i])
        i += 1
    try:
        data = json.loads("".join(cleaned))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"written settings are not JSONC: {exc}\n{text}") from exc
    if not isinstance(data, dict):
        raise AssertionError(f"written settings are not an object\n{text}")
    return data


def _assert_zed_loom(data: dict, *, theme: str | None, other: dict | None) -> None:
    if theme is not None:
        assert data["theme"] == theme
    servers = data["context_servers"]
    assert isinstance(servers, dict)
    if other is not None:
        assert servers["other"] == other
    assert servers["loom"] == {
        "url": "http://localhost:7777/mcp",
        "headers": {"Authorization": "Bearer T"},
    }


def test_zed_install_keeps_a_trailing_comma_beside_the_closing_brace(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text('{\n  "theme": "One Dark",}\n')
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert "wrote" in capsys.readouterr().out
    _assert_zed_loom(_jsonc_object(cfg.read_text()), theme="One Dark", other=None)


# A block comment can hold a newline. The splice follows a parsed token, so
# the new member stays outside the comment.
@pytest.mark.parametrize(
    ("original", "theme", "other"),
    [
        ('{\n  "theme": "One Dark" /* a\n  b */}\n', "One Dark", None),
        ('{\n  "theme": "One Dark", /* a\n  b */}\n', "One Dark", None),
        ("{ /* a\nb */ }\n", None, None),
        (
            '{\n  "context_servers": {\n    "other": {"command": "true"} /* a\n    b */}\n}\n',
            None,
            {"command": "true"},
        ),
        (
            '{\n  "context_servers": {\n    "other": {"command": "true"}, /* a\n    b */}\n}\n',
            None,
            {"command": "true"},
        ),
    ],
)
def test_zed_install_places_loom_outside_a_multiline_block_comment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: str,
    theme: str | None,
    other: dict | None,
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text(original)
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert "wrote" in capsys.readouterr().out
    text = cfg.read_text()
    assert "/* a" in text and "b */" in text
    _assert_zed_loom(_jsonc_object(text), theme=theme, other=other)
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert cfg.read_text() == text


def test_zed_install_replaces_a_visible_loom_entry_beside_a_block_comment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text('{\n  "theme": "One Dark" /* a\n  b */}\n')
    assert cli._install_one(
        "zed", token="token-1", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    once = cfg.read_text()
    assert (
        _jsonc_object(once)["context_servers"]["loom"]["headers"]["Authorization"]
        == "Bearer token-1"
    )
    assert cli._install_one(
        "zed", token="token-1", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert cfg.read_text() == once
    assert cli._install_one(
        "zed", token="token-2", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    twice = cfg.read_text()
    assert "token-1" not in twice
    assert twice.count("token-2") == 1
    assert _jsonc_object(twice)["context_servers"]["loom"]["headers"]["Authorization"] == (
        "Bearer token-2"
    )
    assert _jsonc_object(twice)["theme"] == "One Dark"
    assert "/* a" in twice and "b */" in twice


def test_zed_dry_run_on_a_block_comment_keeps_bytes_and_hides_the_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = '{\n  "theme": "One Dark" /* a\n  b */}\n'
    cfg.write_text(original)
    assert cli._install_one(
        "zed",
        token="super-secret-token",
        server_url="http://localhost:7777",
        name="loom",
        dry_run=True,
    )
    out = capsys.readouterr().out
    assert cfg.read_text() == original
    assert "super-secret-token" not in out
    assert "<token>" in out


def test_zed_install_keeps_a_nested_trailing_comma_beside_the_closing_brace(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text('{\n  "context_servers": {\n    "other": {"command": "true"},}\n}\n')
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert "wrote" in capsys.readouterr().out
    _assert_zed_loom(
        _jsonc_object(cfg.read_text()),
        theme=None,
        other={"command": "true"},
    )


def test_zed_install_adds_context_servers_when_the_key_is_absent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text('// note\n{\n  "theme": "One Dark"\n}\n')
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    text = cfg.read_text()
    assert text.startswith("// note\n")
    assert '"theme": "One Dark"' in text
    assert '"context_servers"' in text
    assert '"url": "http://localhost:7777/mcp"' in text


# U+1F600 is one character. Its JSON spelling is the surrogate pair \uD83D\uDE00.
_NON_BMP = "\U0001f600"
_OTHER_NON_BMP = "\U0001f44d"


def test_zed_install_writes_a_non_bmp_name(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new name outside the BMP is written once. The repeat is the same bytes."""
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text('{\n  "theme": "One Dark"\n}\n')
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name=_NON_BMP, dry_run=False
    )
    text = cfg.read_text()
    raw = cfg.read_bytes()
    assert '"theme": "One Dark"' in text
    assert json.loads('"\\uD83D\\uDE00"') == _NON_BMP
    data = _jsonc_object(text)
    assert list(data["context_servers"]) == [_NON_BMP]
    assert data["context_servers"][_NON_BMP]["headers"]["Authorization"] == "Bearer T"
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name=_NON_BMP, dry_run=False
    )
    assert cfg.read_bytes() == raw


def test_zed_install_updates_a_literal_non_bmp_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The character itself is the key. A token change rewrites that entry only."""
    cfg = _zed_path(tmp_path, monkeypatch)
    other = '"other": {"command": "keep-me", "args": ["stay"]}'
    cfg.write_text(
        "{\n"
        '  "context_servers": {\n'
        f"    {json.dumps(_NON_BMP, ensure_ascii=False)}: "
        '{"url": "http://old", "headers": {"Authorization": "Bearer old-token"}},\n'
        f"    {other}\n"
        "  }\n"
        "}\n"
    )
    assert other.encode() in cfg.read_bytes()
    assert cli._install_one(
        "zed",
        token="new-token",
        server_url="http://localhost:7777",
        name=_NON_BMP,
        dry_run=False,
    )
    text = cfg.read_text()
    assert other in text
    assert "old-token" not in text
    assert text.count("new-token") == 1
    data = _jsonc_object(text)
    assert set(data["context_servers"]) == {_NON_BMP, "other"}
    assert data["context_servers"]["other"] == {"command": "keep-me", "args": ["stay"]}
    assert data["context_servers"][_NON_BMP]["headers"]["Authorization"] == "Bearer new-token"
    again = cfg.read_bytes()
    assert cli._install_one(
        "zed",
        token="new-token",
        server_url="http://localhost:7777",
        name=_NON_BMP,
        dry_run=False,
    )
    assert cfg.read_bytes() == again


@pytest.mark.parametrize("escape", [r"\uD83D\uDE00", r"\uD83d\uDe00"])
def test_zed_install_updates_an_escaped_surrogate_pair(
    tmp_path, monkeypatch: pytest.MonkeyPatch, escape: str
) -> None:
    """The escaped pair is the same key as the character. The other pair stays."""
    cfg = _zed_path(tmp_path, monkeypatch)
    sibling = r'"\uD83D\uDC4D": {"command": "keep-me"}'
    cfg.write_text(
        "{\n"
        '  "context_servers": {\n'
        f'    "{escape}": '
        '{"url": "http://old", "headers": {"Authorization": "Bearer old-token"}},\n'
        f"    {sibling}\n"
        "  }\n"
        "}\n"
    )
    assert cli._install_one(
        "zed",
        token="new-token",
        server_url="http://localhost:7777",
        name=_NON_BMP,
        dry_run=False,
    )
    text = cfg.read_text()
    assert sibling in text
    assert text.count(escape) == 1
    assert "old-token" not in text
    assert text.count("new-token") == 1
    data = _jsonc_object(text)
    assert json.loads('"\\uD83D\\uDE00"') == _NON_BMP
    assert json.loads('"\\uD83D\\uDC4D"') == _OTHER_NON_BMP
    assert set(data["context_servers"]) == {_NON_BMP, _OTHER_NON_BMP}
    assert data["context_servers"][_OTHER_NON_BMP] == {"command": "keep-me"}
    assert len(data["context_servers"]) == 2
    again = cfg.read_bytes()
    assert cli._install_one(
        "zed",
        token="new-token",
        server_url="http://localhost:7777",
        name=_NON_BMP,
        dry_run=False,
    )
    assert cfg.read_bytes() == again


def test_zed_install_refuses_a_literal_key_and_its_escaped_pair(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The character and its surrogate pair are one key. A duplicate is not a write."""
    cfg = _zed_path(tmp_path, monkeypatch)
    original = (
        "{\n"
        '  "context_servers": {\n'
        f"    {json.dumps(_NON_BMP, ensure_ascii=False)}: "
        '{"url": "http://old", "headers": {"Authorization": "Bearer old"}},\n'
        '    "\\uD83D\\uDE00": {"url": "http://other", '
        '"headers": {"Authorization": "Bearer other"}}\n'
        "  }\n"
        "}\n"
    )
    cfg.write_bytes(original.encode())
    before = cfg.read_bytes()
    assert (
        cli._install_one(
            "zed", token="T", server_url="http://localhost:7777", name=_NON_BMP, dry_run=False
        )
        is False
    )
    assert cfg.read_bytes() == before
    err = capsys.readouterr().err
    assert "not changed" in err
    assert "duplicate" in err


@pytest.mark.parametrize(
    "original",
    [
        '{ "note": "\\uD800" }\n',
        '{ "note": "\\uDE00" }\n',
        '{ "note": "\\uD83D\\uD83D" }\n',
        '{ "note": "\\uD83D\\uDE00\\uD800" }\n',
        '{ "context_servers": { "\\uD800": {"command": "true"} } }\n',
    ],
)
def test_zed_install_refuses_an_unpaired_surrogate_without_writing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: str,
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text(original)
    before = cfg.read_bytes()
    assert (
        cli._install_one(
            "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
        )
        is False
    )
    assert cfg.read_bytes() == before
    assert "not changed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "original",
    [
        '{ "note": "\\u12" }\n',
        '{ "note": "\\uXXXX" }\n',
        '{ "note": "\\uD83" }\n',
    ],
)
def test_zed_install_refuses_a_malformed_unicode_escape_without_writing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: str,
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text(original)
    before = cfg.read_bytes()
    assert (
        cli._install_one(
            "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
        )
        is False
    )
    assert cfg.read_bytes() == before
    assert "not changed" in capsys.readouterr().err


def test_zed_dry_run_redacts_a_non_bmp_name_and_writes_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = '{\n  "theme": "One Dark"\n}\n'
    cfg.write_bytes(original.encode())
    before = cfg.read_bytes()
    assert cli._install_one(
        "zed",
        token="super-secret-token",
        server_url="http://localhost:7777",
        name=_NON_BMP,
        dry_run=True,
    )
    captured = capsys.readouterr()
    assert cfg.read_bytes() == before
    assert "super-secret-token" not in captured.out
    assert "super-secret-token" not in captured.err
    assert "<token>" in captured.out


def test_zed_install_keeps_a_legal_surrogate_pair_in_another_value(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legal pair in a different entry stays the bytes it was."""
    cfg = _zed_path(tmp_path, monkeypatch)
    kept = r'"other": {"command": "\uD83D\uDE00"}'
    cfg.write_text('{\n  "context_servers": {\n    ' + kept + "\n  }\n}\n")
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    text = cfg.read_text()
    assert kept in text
    data = _jsonc_object(text)
    assert data["context_servers"]["other"]["command"] == _NON_BMP
    assert "loom" in data["context_servers"]
    assert len(data["context_servers"]) == 2


def test_zed_install_updates_an_escaped_bmp_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single \\uXXXX below U+10000 is that character, and the spelling stays."""
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_text(
        '{\n  "context_servers": {\n    "\\u00e9": '
        '{"url": "http://old", "headers": {"Authorization": "Bearer old"}}\n  }\n}\n'
    )
    name = "\u00e9"
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name=name, dry_run=False
    )
    text = cfg.read_text()
    assert r'"\u00e9"' in text
    data = _jsonc_object(text)
    assert list(data["context_servers"]) == [name]
    assert data["context_servers"][name]["headers"]["Authorization"] == "Bearer T"


def _no_bare_break(raw: bytes) -> None:
    """Every break is CR LF, or every break is LF. A leftover CR or LF is mixed."""
    if b"\r\n" in raw:
        stripped = raw.replace(b"\r\n", b"")
        assert b"\n" not in stripped
        assert b"\r" not in stripped
    else:
        assert b"\r" not in raw


def test_zed_install_keeps_crlf_and_writes_the_new_fragment_as_crlf(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CR LF file stays CR LF, including the lines this command adds."""
    cfg = _zed_path(tmp_path, monkeypatch)
    original = b'{\r\n  "theme": "One Dark",\r\n}\r\n'
    cfg.write_bytes(original)
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    raw = cfg.read_bytes()
    assert b'"theme": "One Dark",\r\n' in raw
    assert b'"url": "http://localhost:7777/mcp"' in raw
    _no_bare_break(raw)
    assert b"\r\n" in raw
    data = _jsonc_object(raw.decode("utf-8"))
    assert data["theme"] == "One Dark"
    assert data["context_servers"]["loom"]["headers"]["Authorization"] == "Bearer T"
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert cfg.read_bytes() == raw


def test_zed_install_keeps_lf_without_adding_cr(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    cfg.write_bytes(b'{\n  "theme": "One Dark",\n}\n')
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    raw = cfg.read_bytes()
    assert b"\r" not in raw
    assert b'"theme": "One Dark",\n' in raw
    assert b'"url": "http://localhost:7777/mcp"' in raw
    again = raw
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert cfg.read_bytes() == again


def test_zed_install_leaves_a_mixed_file_mixed_and_uses_lf_for_the_fragment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mix is not normalized. The new fragment uses LF."""
    cfg = _zed_path(tmp_path, monkeypatch)
    original = b'{\r\n  "theme": "One Dark",\n  "ui": "kept"\n}\n'
    cfg.write_bytes(original)
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    raw = cfg.read_bytes()
    assert raw.startswith(b"{\r\n")
    # The previous member gains a comma. Its LF stays, and no new CR LF appears.
    assert b'"theme": "One Dark",\n' in raw
    assert b'"ui": "kept",\n' in raw
    assert raw.count(b"\r\n") == original.count(b"\r\n")
    assert b'"url": "http://localhost:7777/mcp"' in raw
    data = _jsonc_object(raw.decode("utf-8"))
    assert data["ui"] == "kept"
    assert "loom" in data["context_servers"]


def test_zed_install_changes_only_the_crlf_entry(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    sibling = b'    "other": {"command": "keep-me"}\r\n'
    cfg.write_bytes(
        b'{\r\n  "context_servers": {\r\n'
        b'    "loom": {"url": "http://old", "headers": {"Authorization": "Bearer old-token"}},\r\n'
        + sibling
        + b"  }\r\n}\r\n"
    )
    assert cli._install_one(
        "zed",
        token="new-token",
        server_url="http://localhost:7777",
        name="loom",
        dry_run=False,
    )
    raw = cfg.read_bytes()
    assert sibling in raw
    assert b"old-token" not in raw
    assert raw.count(b"new-token") == 1
    _no_bare_break(raw)
    data = _jsonc_object(raw.decode("utf-8"))
    assert data["context_servers"]["other"] == {"command": "keep-me"}
    assert data["context_servers"]["loom"]["headers"]["Authorization"] == "Bearer new-token"
    again = raw
    assert cli._install_one(
        "zed",
        token="new-token",
        server_url="http://localhost:7777",
        name="loom",
        dry_run=False,
    )
    assert cfg.read_bytes() == again


def test_zed_install_refuses_broken_crlf_without_writing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = b'{ "theme": "One Dark",\r\n'
    cfg.write_bytes(original)
    assert (
        cli._install_one(
            "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
        )
        is False
    )
    assert cfg.read_bytes() == original
    assert "not changed" in capsys.readouterr().err


def test_zed_dry_run_keeps_crlf_bytes_and_hides_the_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = b'{\r\n  "theme": "One Dark",\r\n}\r\n'
    cfg.write_bytes(original)
    assert cli._install_one(
        "zed",
        token="super-secret-token",
        server_url="http://localhost:7777",
        name="loom",
        dry_run=True,
    )
    captured = capsys.readouterr()
    assert cfg.read_bytes() == original
    assert b"super-secret-token" not in cfg.read_bytes()
    assert "super-secret-token" not in captured.out
    assert "super-secret-token" not in captured.err
    assert "<token>" in captured.out


def test_zed_install_writes_a_missing_file_with_lf(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    raw = cfg.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    assert _jsonc_object(raw.decode("utf-8"))["context_servers"]["loom"]["url"].endswith("/mcp")


def _text_stream(path, text: str, newline: str | None) -> bytes:
    with path.open("w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return path.read_bytes()


def _zed_round(cfg, original: bytes, *, keep: bytes) -> None:
    cfg.write_bytes(original)
    assert cli._install_one(
        "zed", token="old-token", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    once = cfg.read_bytes()
    assert keep in once
    assert cli._install_one(
        "zed", token="old-token", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert cfg.read_bytes() == once
    assert cli._install_one(
        "zed", token="new-token", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    twice = cfg.read_bytes()
    assert keep in twice
    assert b"old-token" not in twice
    assert twice.count(b"new-token") == 1


def test_zed_install_keeps_lf_bytes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = b'{\n  "theme": "One Dark",\n}\n'
    _zed_round(cfg, original, keep=b'{\n  "theme": "One Dark",')
    assert b"\r" not in cfg.read_bytes()


def test_zed_install_keeps_crlf_bytes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = b'{\r\n  "theme": "One Dark",\r\n}\r\n'
    _zed_round(cfg, original, keep=b'{\r\n  "theme": "One Dark",')
    raw = cfg.read_bytes()
    assert b"\r\r\n" not in raw
    streamed = _text_stream(tmp_path / "crlf-stream.json", raw.decode("utf-8"), "\r\n")
    assert streamed != raw
    assert streamed.count(b"\r") > raw.count(b"\r")


def test_zed_install_keeps_mixed_bytes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = b'{\r\n  "theme": "One Dark",\n  "ui": "kept"\n}\n'
    _zed_round(cfg, original, keep=b'"ui": "kept",\n')
    raw = cfg.read_bytes()
    assert raw.startswith(b"{\r\n")
    assert raw.count(b"\r\n") == 1


def test_zed_install_keeps_a_bom_and_crlf(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _zed_path(tmp_path, monkeypatch)
    original = b'\xef\xbb\xbf{\r\n  "theme": "One Dark",\r\n}\r\n'
    _zed_round(cfg, original, keep=b'\xef\xbb\xbf{\r\n  "theme": "One Dark",')
    raw = cfg.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw.replace(b"\r\n", b"")


def test_only_zed_settings_writes_skip_newline_translation(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str | None] = []
    real = cli.write_private

    def wrap(path, text, *args, **kwargs):
        seen.append(kwargs.get("newline", None))
        return real(path, text, *args, **kwargs)

    monkeypatch.setattr(cli, "write_private", wrap)
    zed = _zed_path(tmp_path, monkeypatch)
    zed.write_bytes(b'{\r\n  "theme": "One Dark",\r\n}\r\n')
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert seen == [""]
    assert b"\r\r\n" not in zed.read_bytes()
    seen.clear()
    fresh = tmp_path / "fresh.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda _client: fresh)
    assert cli._install_one(
        "zed", token="T", server_url="http://localhost:7777", name="loom", dry_run=False
    )
    assert seen == [""]
    seen.clear()
    cursor = tmp_path / "mcp.json"
    monkeypatch.setattr(cli, "_resolve_install_path", lambda _client: cursor)
    assert cli._install_one("cursor", token="T", server_url="https://h", name="loom", dry_run=False)
    assert seen == [None]
    cursor_text = cursor.read_text(encoding="utf-8")
    assert cursor.read_bytes() == _text_stream(tmp_path / "cursor-platform.json", cursor_text, None)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli._install_claude_code_capture("https://h", "secret-token", dry_run=False)
    assert cli._install_codex_capture("https://h", "secret-token", dry_run=False)
    assert seen[1:] == [None, None]
    capsys.readouterr()


@pytest.mark.parametrize(
    "text",
    ['{"note":1\u0661}', '{"note":' + ("1" * 5000) + "}"],
)
def test_a_non_ascii_or_huge_number_is_a_jsonc_edit_error(text: str) -> None:
    with pytest.raises(JsoncEditError):
        upsert_object_key(text, "context_servers", "loom", {"url": "http://h"})


@pytest.mark.parametrize(
    "original",
    ['{"note":1\u0661}', '{"note":' + ("1" * 5000) + "}"],
)
def test_zed_refuses_a_bad_number_and_still_installs_cursor(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], original: str
) -> None:
    import argparse

    zed = tmp_path / "settings.json"
    cursor = tmp_path / "mcp.json"
    before = original.encode()
    zed.write_bytes(before)

    def resolve(client: str):
        return zed if client == "zed" else cursor

    monkeypatch.setattr(cli, "_resolve_install_path", resolve)
    parser = argparse.ArgumentParser()
    cli._add_install_flags(parser)
    args = parser.parse_args(
        ["--client", "zed", "cursor", "--token", "T", "--server-url", "http://localhost:7777"]
    )
    assert cli._cmd_install(args) == 1
    assert zed.read_bytes() == before
    err = capsys.readouterr().err
    assert "The file was not changed." in err
    assert "http://localhost:7777/mcp" in err
    assert json.loads(cursor.read_text())["mcpServers"]["loom"]["headers"]["Authorization"] == (
        "Bearer T"
    )
