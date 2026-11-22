"""Native MCP configuration and credentials for isolated Harbor environments."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .scopes import binding_for

PROXY_ENV = dict.fromkeys(
    ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"), ""
)


def token_variable(order: int) -> str:
    return f"LOOM_ATTEMPT_TOKEN_{order}"


def agent_settings(protocol: dict, slot: dict) -> str:
    settings = '[agent]\nuser = "agent"\n'
    if slot["mode"] == "baseline":
        settings += 'network_mode = "no-network"\n'
    else:
        hostname = urlsplit(protocol["loom_url"]).hostname
        if not hostname:
            raise ValueError("Native MCP requires an explicit Loom hostname")
        settings += 'network_mode = "allowlist"\nallowed_hosts = ' + json.dumps([hostname]) + "\n"
    return settings


def mcp_settings(protocol: dict, slot: dict) -> str:
    binding = binding_for(protocol, slot)
    if slot["mode"] == "baseline":
        return ""
    headers = {
        "Authorization": "Bearer ${" + token_variable(slot["order"]) + "}",
        "X-Loom-Code-Scope": binding["org"] + "/" + binding["code_namespace"],
        # The pinned egress proxy reuses closed upstream HTTP sockets after idle periods.
        "Connection": "close",
    }
    return (
        '\n[[environment.mcp_servers]]\nname = "loom"\ntransport = "streamable-http"\nurl = '
        + json.dumps(protocol["loom_url"].rstrip("/") + "/mcp?toolset=coding")
        + "\n[environment.mcp_servers.headers]\n"
        + "".join(
            json.dumps(name) + " = " + json.dumps(value) + "\n" for name, value in headers.items()
        )
    )


def load_credentials(protocol: dict) -> None:
    """Harbor resolves each server's header references and injects only those values."""
    for slot in protocol["schedule"]:
        binding = binding_for(protocol, slot)
        if slot["mode"] == "baseline":
            continue
        path = Path(binding["token_file"])
        if path.is_symlink() or not path.is_file():
            raise PermissionError("Attempt credential must be an owned regular file")
        value = path.read_text().strip()
        if not value:
            raise PermissionError("Attempt credential is empty")
        os.environ[token_variable(slot["order"])] = value
