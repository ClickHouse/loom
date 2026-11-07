"""The MCP client setups a new token is handed with (`me_service_accounts.create`):
one config entry per supported client, built from the token and the server's MCP URL."""

from __future__ import annotations

# stdio-only clients need the `mcp-remote` npx shim to reach a remote HTTP MCP server;
# the rest speak remote MCP natively (URL + headers, with `type: "http"` for
# claude-code / vscode). When a client gains native HTTP support, drop it from here.
_STDIO_VIA_NPX = {"claude-desktop", "zed", "cline"}

# Coding agents mount the `coding` toolset, on the URL, as the dashboard's snippets do
# (frontend/src/lib/clients.ts CODING_TOOLSET_CLIENTS).
_CODING_CLIENTS = {"claude-code", "codex", "cursor"}


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
    return mcp_url if "toolset=" in mcp_url else f"{mcp_url}?toolset=coding"


def client_config(client: str, mcp_url: str, minted: str) -> dict[str, object]:
    """One MCP client's setup for a token: the config-file key, the server entry
    (URL + bearer, the `mcp-remote` bridge for stdio clients,
    `bearer_token_env_var` for Codex), the file's own text when it is not JSON,
    the one-line registration command, and the file's path per platform."""
    key = config_key(client)
    auth_header = f"Authorization: Bearer {minted}"
    if client in _STDIO_VIA_NPX:
        entry: dict[str, object] = {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote",
                mcp_url,
                "--allow-http",
                "--header",
                auth_header,
            ],
        }
    elif client == "codex":
        # Codex names the env var the bearer lives in; the token itself never sits in
        # config.toml.
        entry = {"url": _coding_url(mcp_url), "bearer_token_env_var": "LOOM_API_KEY"}
    else:
        entry = {
            "url": _coding_url(mcp_url) if client in _CODING_CLIENTS else mcp_url,
            "headers": {"Authorization": f"Bearer {minted}"},
        }
        if client in {"claude-code", "vscode"}:
            entry["type"] = "http"
    config_block = {key: {"loom": entry}}
    config_toml = (
        f'[mcp_servers.loom]\nurl = "{entry["url"]}"\nbearer_token_env_var = "LOOM_API_KEY"\n'
        if client == "codex"
        else ""
    )
    if client == "codex":
        command = f'codex mcp add loom --url "{entry["url"]}" --bearer-token-env-var LOOM_API_KEY'
    elif client == "claude-code":
        # User scope, kept in ~/.claude.json; the dashboard prints the same line.
        command = (
            f'claude mcp add --transport http loom "{entry["url"]}" -H "{auth_header}" -s user'
        )
    else:
        command = ""

    return {
        "config_key": key,
        "config": config_block,
        "config_toml": config_toml,
        "command": command,
        "config_path": CONFIG_PATHS[client],
    }


def client_configs(mcp_url: str, minted: str) -> dict[str, dict[str, object]]:
    """Every supported client's setup for the token `minted`, keyed by client."""
    return {client: client_config(client, mcp_url, minted) for client in MCP_CLIENTS}
