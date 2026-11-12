"""The MCP client setups a new token is handed with (`me_service_accounts.create`):
one config entry per supported client, built from the token and the server's MCP URL."""

from __future__ import annotations

import re

# stdio-only clients need the `mcp-remote` npx shim to reach a remote HTTP MCP server;
# the rest speak remote MCP natively (URL + headers, with `type: "http"` for
# claude-code / vscode). When a client gains native HTTP support, drop it from here.
_STDIO_VIA_NPX = {"claude-desktop", "zed", "cline"}

# Coding agents mount the `coding` toolset, on the URL, as the dashboard's snippets do
# (frontend/src/lib/clients.ts CODING_TOOLSET_CLIENTS) and `loom install` does.
CODING_CLIENTS = frozenset({"claude-code", "codex", "cursor"})
CODING_TOOLSET = "coding"


# Where each client keeps its MCP config, per platform. `loom install` writes the same
# files (server/cli/install.py).
CONFIG_PATHS = {
    "claude-desktop": {
        "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
        "linux": "~/.config/Claude/claude_desktop_config.json",
        "win32": "%APPDATA%\\Claude\\claude_desktop_config.json",
    },
    "claude-code": {
        "darwin": "~/.claude.json",
        "linux": "~/.claude.json",
        "win32": "%USERPROFILE%\\.claude.json",
    },
    "codex": {
        "darwin": "~/.codex/config.toml",
        "linux": "~/.codex/config.toml",
        "win32": "%USERPROFILE%\\.codex\\config.toml",
    },
    "cursor": {
        "darwin": "~/.cursor/mcp.json",
        "linux": "~/.cursor/mcp.json",
        "win32": "%USERPROFILE%\\.cursor\\mcp.json",
    },
    "vscode": {
        "darwin": "~/Library/Application Support/Code/User/mcp.json",
        "linux": "~/.config/Code/User/mcp.json",
        "win32": "%APPDATA%\\Code\\User\\mcp.json",
    },
    "cline": {
        "darwin": "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",  # noqa: E501
        "linux": "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",  # noqa: E501
        "win32": "%APPDATA%\\Code\\User\\globalStorage\\saoudrizwan.claude-dev\\settings\\cline_mcp_settings.json",  # noqa: E501
    },
    "windsurf": {
        "darwin": "~/.codeium/windsurf/mcp_config.json",
        "linux": "~/.codeium/windsurf/mcp_config.json",
        "win32": "%USERPROFILE%\\.codeium\\windsurf\\mcp_config.json",
    },
    "zed": {
        "darwin": "~/.config/zed/mcp.json",
        "linux": "~/.config/zed/mcp.json",
        "win32": "%APPDATA%\\Zed\\mcp.json",
    },
}


# The MCP clients a new token comes with a setup for, one per row of the table above
# (frontend/src/lib/clients.ts mirrors the list).
MCP_CLIENTS = tuple(CONFIG_PATHS)

# The config file's key for its MCP servers; every other client uses "mcpServers".
_CONFIG_KEYS = {"vscode": "servers", "zed": "context_servers", "codex": "mcp_servers"}


def config_key(client: str) -> str:
    return _CONFIG_KEYS.get(client, "mcpServers")


def _coding_url(mcp_url: str) -> str:
    # The dashboard's withToolset, kept alike for the fixture: a URL that already names a
    # toolset keeps it; otherwise toolset=coding is appended.
    if re.search(r"[?&]toolset=", mcp_url):
        return mcp_url
    return f"{mcp_url}{'&' if '?' in mcp_url else '?'}toolset={CODING_TOOLSET}"


def mcp_entry(client: str, url: str, minted: str) -> dict[str, object]:
    """The `loom` entry a client's MCP config holds for `url`: the `mcp-remote` bridge
    for a stdio client, the bearer's env var for Codex, else the URL and its header."""
    if client == "codex":
        # Codex reads the bearer from an env var; the token never sits in config.toml.
        return {"url": url, "bearer_token_env_var": "LOOM_API_KEY"}
    if client in _STDIO_VIA_NPX:
        header = f"Authorization: Bearer {minted}"
        return {
            "command": "npx",
            "args": ["-y", "mcp-remote", url, "--allow-http", "--header", header],
        }
    entry: dict[str, object] = {"url": url, "headers": {"Authorization": f"Bearer {minted}"}}
    if client in {"claude-code", "vscode"}:
        entry["type"] = "http"
    return entry


def client_config(client: str, mcp_url: str, minted: str) -> dict[str, object]:
    """One MCP client's setup for a token: the config-file key and entry (coding agents
    on the `coding` toolset), the file's own text when it is not JSON, the one-line
    registration command, the variables to export before the client starts, and the
    file's path per platform."""
    url = _coding_url(mcp_url) if client in CODING_CLIENTS else mcp_url
    toml, command, env = "", "", {}
    if client == "codex":
        toml = f'[mcp_servers.loom]\nurl = "{url}"\nbearer_token_env_var = "LOOM_API_KEY"\n'
        command = f'codex mcp add loom --url "{url}" --bearer-token-env-var LOOM_API_KEY'
        env = {"LOOM_API_KEY": minted}
    elif client == "claude-code":
        # User scope, kept in ~/.claude.json; the dashboard prints the same line.
        header = f"Authorization: Bearer {minted}"
        command = f'claude mcp add --transport http loom "{url}" -H "{header}" -s user'
    key = config_key(client)
    return {
        "config_key": key,
        "config": {key: {"loom": mcp_entry(client, url, minted)}},
        "config_toml": toml,
        "command": command,
        "env": env,
        "config_path": CONFIG_PATHS[client],
    }


def client_configs(mcp_url: str, minted: str) -> dict[str, dict[str, object]]:
    """Every supported client's setup for the token `minted`, keyed by client."""
    return {client: client_config(client, mcp_url, minted) for client in MCP_CLIENTS}
