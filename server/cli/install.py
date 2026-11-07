"""`loom install`: wire Loom into a local AI client's config (Claude Desktop,
Cursor, VS Code, Claude Code, Codex), plus the capture hooks.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from server.cli._client import DEFAULT_BASE_URL, agent_mode, default_base_url, resolve_token
from server.cli._paths import write_private
from server.identity.app.token_clients import CONFIG_PATHS, config_key, mcp_entry

# ============================================================
# `loom install` — wire Loom into a local AI client's config
# ============================================================

# Claude Code keeps its MCP servers in ~/.claude.json through `claude mcp add`
# (_install_claude_code_mcp), so for it the path is only a probe: ~/.claude exists once
# it has run.
_CLAUDE_CODE_PROBE = {
    "darwin": "~/.claude/settings.json",
    "linux": "~/.claude/settings.json",
    "win32": "%USERPROFILE%\\.claude\\settings.json",
}
# Per-client config file: the token mint's table, so the dashboard and `loom install`
# name the same files.
_INSTALL_CLIENTS: dict[str, dict[str, str]] = {**CONFIG_PATHS, "claude-code": _CLAUDE_CODE_PROBE}


def _resolve_install_path(client: str) -> Path:
    """Map (client, current OS) → expanded config file path."""
    spec = _INSTALL_CLIENTS[client]
    osname = sys.platform
    # Unknown platforms fall back to the linux convention.
    key = "darwin" if osname == "darwin" else "win32" if osname == "win32" else "linux"
    raw = spec[key]
    # Expand both ~ (home) and %ENV% (Windows) forms regardless of host OS.
    raw = os.path.expanduser(raw)
    raw = os.path.expandvars(raw)
    return Path(raw)


def _detect_installed_clients() -> list[str]:
    """Best-effort detection: a client counts as present if its config
    directory exists. Lets `loom install` (no --client) wire every editor the
    user actually has, in one pass, instead of one-at-a-time. Conservative — a
    missing dir just means "skip", never an error."""
    found: list[str] = []
    for client in _INSTALL_CLIENTS:
        try:
            if _resolve_install_path(client).parent.exists():
                found.append(client)
        except Exception:  # noqa: BLE001 — detection must never crash install
            continue
    return found


def _toolset_arg(value: str) -> str:
    """`--toolset`: a listing name, or a `+` union of curated rosters
    (`coding+tracker`); '' keeps the client's default."""
    from server.operations.toolsets import MCP_TOOLSETS, canonical_toolset, is_toolset

    v = canonical_toolset(value)
    if not v or is_toolset(v):
        return v
    raise argparse.ArgumentTypeError(
        f"{value!r} is not a toolset: one of {', '.join(MCP_TOOLSETS)} or a `+` union "
        "of curated rosters such as coding+tracker"
    )


def _install_mcp_url(server_url: str, toolset: str) -> str:
    """The MCP URL a client mounts: `<base>/mcp`, plus `?toolset=` when one is
    named (the toolset rides the URL so one deployment serves a
    chat client `core` and a coding client `coding`)."""
    mcp_url = server_url.rstrip("/") + "/mcp"
    if toolset:
        mcp_url += f"?toolset={toolset}"
    return mcp_url


# Claude Code is a coding agent: its listing defaults to the `coding` tier
# (19 tools) rather than the deployment default `core` (41 with the code tools
# on) — the same choice the coding-agent plugin's .mcp.json makes.
_CLAUDE_CODE_DEFAULT_TOOLSET = "coding"


def _shell_words(argv: list[str]) -> str:
    """Render argv as one copy-pasteable line: bare words stay bare, anything
    with a space or URL punctuation is double-quoted (works in sh, zsh, and
    PowerShell alike; shlex.join's single quotes do not)."""
    import re as _re

    def _word(a: str) -> str:
        if _re.fullmatch(r"[A-Za-z0-9_./:=+-]+", a):
            return a
        # A `${VAR}` reference is for the client to expand, not the shell:
        # single quotes keep it literal in sh, zsh and PowerShell.
        if "$" in a and "'" not in a:
            return "'" + a + "'"
        return '"' + a.replace('"', '\\"') + '"'

    return " ".join(_word(a) for a in argv)


# Claude Code expands `${VAR}` in an HTTP server's headers at launch, from its
# own environment including ~/.claude/settings.json `env` (verified on 2.1.283),
# which `_install_claude_code_capture` fills with LOOM_API_KEY. So the header it
# stores names the variable and the token never rides `claude mcp add`'s argv
# (visible in `ps`) or a printed command line.
_CLAUDE_CODE_TOKEN_ENV = "LOOM_API_KEY"
_CLAUDE_CODE_AUTH = "Bearer ${" + _CLAUDE_CODE_TOKEN_ENV + "}"


def _claude_mcp_add_argv(*, name: str, mcp_url: str) -> list[str]:
    """`claude mcp add` for a streamable-HTTP server at user scope — the form
    Claude Code stores in ~/.claude.json."""
    return [
        "claude",
        "mcp",
        "add",
        "--transport",
        "http",
        name,
        mcp_url,
        "-H",
        f"Authorization: {_CLAUDE_CODE_AUTH}",
        "-s",
        "user",
    ]


def _claude_code_project_block(*, name: str, mcp_url: str) -> dict[str, Any]:
    """The project-scoped alternative: a `.mcp.json` at the repo root, which
    Claude Code reads for every session started in that directory."""
    return {
        "mcpServers": {
            name: {
                "type": "http",
                "url": mcp_url,
                "headers": {"Authorization": _CLAUDE_CODE_AUTH},
            }
        }
    }


def _plugin_mounts_the_server(force: bool) -> bool:
    """With the loom plugin installed, Claude
    Code already mounts the server from the plugin's `.mcp.json`; a
    user-scope entry on top lists every tool twice and its copy carries no
    code scope. Skip it (and say why) unless --force."""
    from pathlib import Path

    from server.cli.doctor_client import claude_plugin_installed

    if force or not claude_plugin_installed(Path.home()):
        return False
    print(
        "claude-code: the loom plugin is installed and mounts the server itself — not "
        "adding a user-scope entry (it would list every Loom tool twice). Pass --force "
        "to add it anyway."
    )
    return True


def _install_claude_code_mcp(
    *, server_url: str, name: str, dry_run: bool, toolset: str, force: bool = False
) -> bool:
    """Wire Claude Code's MCP entry the way Claude Code itself does it: run
    `claude mcp add … -s user` when the `claude` binary is on PATH, else print
    that exact command for the user to run; print the project `.mcp.json`
    alternative either way. Never writes ~/.claude/mcp.json — Claude Code does
    not read that file."""
    import json as _json
    import shutil
    import subprocess

    if _plugin_mounts_the_server(force):
        return True
    mcp_url = _install_mcp_url(server_url, toolset or _CLAUDE_CODE_DEFAULT_TOOLSET)
    argv = _claude_mcp_add_argv(name=name, mcp_url=mcp_url)
    line = _shell_words(argv)
    claude_bin = shutil.which("claude")
    ok = True
    if dry_run:
        print(f"would run: {line}")
    elif claude_bin:
        rc = subprocess.run(argv, check=False).returncode
        if rc == 0:
            print(f"✓ claude-code: registered {name} via `claude mcp add` (user scope)")
        else:
            print(
                f"ERROR (claude-code): `claude mcp add` exited {rc}; re-run it by hand:\n"
                f"    {line}",
                file=sys.stderr,
            )
            ok = False
    else:
        print("claude-code: `claude` is not on PATH — run this once Claude Code is installed:")
        print(f"    {line}")
    print("  or, per project, put this in <repo>/.mcp.json:")
    block = _claude_code_project_block(name=name, mcp_url=mcp_url)
    print("    " + _json.dumps(block, indent=2).replace("\n", "\n    "))
    print(
        f"  Claude Code reads the token from ${_CLAUDE_CODE_TOKEN_ENV} at launch "
        "(`loom install` writes it into ~/.claude/settings.json `env`)."
    )
    return ok


# Codex reads the bearer for a streamable-HTTP server from an environment
# variable it is told the NAME of (`bearer_token_env_var`); the token itself
# never sits in config.toml. LOOM_API_KEY is the name the hooks already read.
_CODEX_TOKEN_ENV = "LOOM_API_KEY"


def _codex_mcp_add_argv(*, name: str, mcp_url: str) -> list[str]:
    """`codex mcp add` for a streamable-HTTP server — the form Codex stores as
    `[mcp_servers.<name>]` in ~/.codex/config.toml."""
    return [
        "codex",
        "mcp",
        "add",
        name,
        "--url",
        mcp_url,
        "--bearer-token-env-var",
        _CODEX_TOKEN_ENV,
    ]


def _codex_config_toml(*, name: str, mcp_url: str) -> str:
    """The same entry as TOML, for a hand edit of ~/.codex/config.toml."""
    return f'[mcp_servers.{name}]\nurl = "{mcp_url}"\nbearer_token_env_var = "{_CODEX_TOKEN_ENV}"\n'


def _install_codex_mcp(
    *, token: str, server_url: str, name: str, dry_run: bool, toolset: str
) -> bool:
    """Wire Codex's MCP entry the way Codex itself does it: run `codex mcp add
    <name> --url … --bearer-token-env-var LOOM_API_KEY` when the `codex` binary
    is on PATH, else print that exact command; print the config.toml form
    either way, and the `export LOOM_API_KEY=…` line Codex needs at launch.
    Never edits config.toml directly — it is Codex's whole state file."""
    import shutil
    import subprocess

    mcp_url = _install_mcp_url(server_url, toolset or _CLAUDE_CODE_DEFAULT_TOOLSET)
    argv = _codex_mcp_add_argv(name=name, mcp_url=mcp_url)
    line = _shell_words(argv)
    codex_bin = shutil.which("codex")
    ok = True
    if dry_run:
        print(f"would run: {line}")
    elif codex_bin:
        rc = subprocess.run(argv, check=False).returncode
        if rc == 0:
            print(f"✓ codex: registered {name} via `codex mcp add` (~/.codex/config.toml)")
        else:
            print(
                f"ERROR (codex): `codex mcp add` exited {rc}; re-run it by hand:\n    {line}",
                file=sys.stderr,
            )
            ok = False
    else:
        print("codex: `codex` is not on PATH — run this once Codex is installed:")
        print(f"    {line}")
    print("  or put this in ~/.codex/config.toml:")
    print("    " + _codex_config_toml(name=name, mcp_url=mcp_url).replace("\n", "\n    ").rstrip())
    print(f"  Codex reads the token from ${_CODEX_TOKEN_ENV} at launch; add to your shell profile:")
    print(f"    export {_CODEX_TOKEN_ENV}={_shell_words([token])}")
    return ok


def _install_codex_capture(server_url: str, token: str, *, dry_run: bool) -> bool:
    """The Codex capture plugin (hooks + skills) reads its server URL and token
    from ~/.loom/config.json — the one file the hooks of both coding-agent
    plugins fall back to when LOOM_BASE_URL / LOOM_API_KEY are not exported. Write
    url + token there (0600, merging what exists) and print the two plugin
    commands; this CLI cannot run `codex plugin add` itself."""
    import json as _json

    path = Path(os.path.expanduser("~/.loom/config.json"))
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                existing = _json.load(f)
            if not isinstance(existing, dict):
                print(
                    f"ERROR (codex capture): {path} top level is not a JSON object; "
                    "skipping the plugin config.",
                    file=sys.stderr,
                )
                return False
        except _json.JSONDecodeError as e:
            print(
                f"ERROR (codex capture): {path} is not valid JSON ({e}); "
                "skipping the plugin config.",
                file=sys.stderr,
            )
            return False
    existing["url"] = server_url.rstrip("/")
    existing["token"] = token
    rendered = _json.dumps(existing, indent=2) + "\n"
    if dry_run:
        _print_dry_run(path, {"url": existing["url"], "token": token}, token)
    else:
        write_private(path, rendered)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print(f"✓ codex capture: wrote url + token → {path} (0600)")
    print("  then install the capture plugin (hooks + skills):")
    print("    codex plugin marketplace add ClickHouse/loom")
    print("    codex plugin add loom@loom")
    return True


def _print_dry_run(path: Path, changes: dict[str, Any], token: str) -> None:
    """The preview of a config write: the keys this command sets, the token
    shown as `<token>`, never the whole merged file — it holds this token and
    whatever other credentials the file already carries (other servers'
    headers, provider keys in an `env` block), and this lands in terminals and
    logs."""
    import json as _json

    rendered = _json.dumps(changes, indent=2)
    if token:
        rendered = rendered.replace(token, "<token>")
    print(f"would write to: {path}")
    print(rendered)
    print("  (every other key in the file is kept as it is)")


def _install_one(
    client: str,
    *,
    token: str,
    server_url: str,
    name: str,
    dry_run: bool,
    toolset: str = "",
    force: bool = False,
) -> bool:
    """Splice the Loom MCP entry into ONE client's config — atomic merge (temp
    file + rename), never clobbers unrelated keys. Prints its own success/error
    line and returns True on success so the caller can aggregate across clients
    without one bad config aborting the rest. Claude Code has no such file; it
    goes through `claude mcp add`."""
    import json as _json

    if client == "claude-code":
        return _install_claude_code_mcp(
            server_url=server_url,
            name=name,
            dry_run=dry_run,
            toolset=toolset,
            force=force,
        )
    if client == "codex":
        return _install_codex_mcp(
            token=token, server_url=server_url, name=name, dry_run=dry_run, toolset=toolset
        )
    section = config_key(client)
    cfg_path = _resolve_install_path(client)
    mcp_url = _install_mcp_url(server_url, toolset)
    entry = mcp_entry(client, mcp_url, token)

    existing: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            with cfg_path.open("r", encoding="utf-8") as f:
                existing = _json.load(f)
            if not isinstance(existing, dict):
                print(
                    f"ERROR ({client}): {cfg_path} top level is not a JSON "
                    f"object; refusing to overwrite — inspect/delete it first.",
                    file=sys.stderr,
                )
                return False
        except _json.JSONDecodeError as e:
            print(
                f"ERROR ({client}): {cfg_path} is not valid JSON ({e}); "
                f"fix or delete it and re-run.",
                file=sys.stderr,
            )
            return False

    section_map = existing.get(section)
    if not isinstance(section_map, dict):
        section_map = {}
    section_map[name] = entry
    existing[section] = section_map
    rendered = _json.dumps(existing, indent=2) + "\n"

    if dry_run:
        _print_dry_run(cfg_path, {section: {name: entry}}, token)
        return True

    write_private(cfg_path, rendered)
    print(f"✓ {client}: wrote {name} → {cfg_path}")
    return True


def _install_claude_code_capture(server_url: str, token: str, *, dry_run: bool) -> bool:
    """Configure the Claude Code capture *plugin* (conversation hooks) by
    writing LOOM_BASE_URL + LOOM_API_KEY into ~/.claude/settings.json's `env`, which
    the plugin's hooks inherit. The MCP entry written above is the separate
    tools surface; this is what turns on background conversation capture. The
    plugin itself installs via `claude plugin install` (this CLI can't run it),
    so that's printed as the final step. namespace/org come from the token."""
    import json as _json

    path = Path(os.path.expanduser("~/.claude/settings.json"))
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                existing = _json.load(f)
            if not isinstance(existing, dict):
                print(
                    f"ERROR (claude-code capture): {path} top level is not a "
                    f"JSON object; skipping the plugin env config.",
                    file=sys.stderr,
                )
                return False
        except _json.JSONDecodeError as e:
            print(
                f"ERROR (claude-code capture): {path} is not valid JSON ({e}); "
                f"skipping the plugin env config.",
                file=sys.stderr,
            )
            return False

    env = existing.get("env")
    if not isinstance(env, dict):
        env = {}
    env["LOOM_BASE_URL"] = server_url.rstrip("/")
    env["LOOM_API_KEY"] = token
    existing["env"] = env
    rendered = _json.dumps(existing, indent=2) + "\n"

    if dry_run:
        _print_dry_run(path, {"env": {k: env[k] for k in ("LOOM_BASE_URL", "LOOM_API_KEY")}}, token)
    else:
        write_private(path, rendered)
        print(f"✓ claude-code capture: wrote LOOM_BASE_URL + LOOM_API_KEY → {path} (env)")
    print("  then install the capture plugin (hooks):")
    print("    claude plugin marketplace add ClickHouse/loom")
    print("    claude plugin install loom@loom")
    return True


_INSTALL_HELP = (
    "Wire Loom into a local AI client's config. Writes the MCP server block "
    "into Claude Desktop / Cursor / VS Code's config file, atomically, merging "
    "with anything already there; for Claude Code it runs (or prints) "
    "`claude mcp add` and for Codex `codex mcp add`. Restart the client afterwards."
)


def _add_install_flags(p_install: argparse.ArgumentParser) -> None:
    """`loom install`'s flags (kept out of `main`, which is at its length
    baseline)."""
    p_install.add_argument(
        "--client",
        nargs="+",
        default=None,
        choices=list(_INSTALL_CLIENTS),
        help="Which client(s) to wire. Omit to auto-detect and wire every "
        "supported client found on this machine.",
    )
    p_install.add_argument(
        "--server-url",
        default=default_base_url(),
        help=f"Loom server URL (default LOOM_BASE_URL, else {DEFAULT_BASE_URL}).",
    )
    p_install.add_argument(
        "--token",
        default=None,
        help=(
            "JWT to embed in the client config. Defaults to LOOM_API_KEY, then "
            "~/.loom/token. Use `loom mint-token` to generate one."
        ),
    )
    p_install.add_argument(
        "--name",
        default="loom",
        help="Server name: the key under mcpServers (cursor) or servers (vscode), "
        "and the name `claude mcp add` / `codex mcp add` registers.",
    )
    p_install.add_argument(
        "--toolset",
        default="",
        type=_toolset_arg,
        help=(
            "MCP toolset the client lists, as `?toolset=` on the URL: "
            "`coding` for a coding agent, empty for the server default (core; "
            "claude-code and codex default to `coding`)."
        ),
    )
    p_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching the file.",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="claude-code: add the user-scope MCP entry even when the loom plugin is "
        "installed (it mounts the server itself; both = every tool listed twice).",
    )


def _cmd_install(args: argparse.Namespace) -> int:
    """Wire Loom into one, several, or all detected AI clients. With no
    --client, auto-detects every supported editor present and wires them in a
    single pass; a per-client failure is reported and does not abort the rest.
    """
    if agent_mode():
        # Any token installed here would become the hooks' LOOM_API_KEY and stand
        # in for the agent's own; its exchanged token lives minutes.
        print(
            "ERROR: not installing in agent mode: the plugin's hooks exchange this agent's "
            "workload token themselves, and an installed token would replace it. To install "
            "a person's token, unset LOOM_CLIENT_ID first.",
            file=sys.stderr,
        )
        return 2
    token, _ = resolve_token(args.token)
    if not token:
        print(
            "ERROR: --token is required (or set LOOM_API_KEY, or save one to ~/.loom/token). "
            "Use `loom mint-token --subject you@example.com --org default "
            "--namespaces my-ns --roles writer` to generate one.",
            file=sys.stderr,
        )
        return 2

    clients = list(args.client) if args.client else _detect_installed_clients()
    if not clients:
        print(
            "No supported AI clients detected (looked for: "
            + ", ".join(_INSTALL_CLIENTS)
            + "). Pass --client <name> to wire one explicitly.",
            file=sys.stderr,
        )
        return 5
    if not args.client:
        print("Auto-detected client(s): " + ", ".join(clients))

    failed = [
        c
        for c in clients
        if not _install_one(
            c,
            token=token,
            server_url=args.server_url,
            name=args.name,
            dry_run=args.dry_run,
            toolset=args.toolset,
            force=bool(getattr(args, "force", False)),
        )
    ]
    # Claude Code also has a capture *plugin* (conversation hooks); wire its env
    # (LOOM_BASE_URL + LOOM_API_KEY) so background recording works, not just the tools.
    if "claude-code" in clients:
        _install_claude_code_capture(args.server_url, token, dry_run=args.dry_run)
    # Codex's capture plugin reads ~/.loom/config.json.
    if "codex" in clients:
        _install_codex_capture(args.server_url, token, dry_run=args.dry_run)
    if not args.dry_run and len(failed) < len(clients):
        print(f"\nRestart the affected client(s) to load Loom (server: {args.server_url}/mcp).")
    if failed:
        print(
            f"\n{len(failed)} of {len(clients)} client(s) failed: " + ", ".join(failed),
            file=sys.stderr,
        )
        return 1
    return 0
