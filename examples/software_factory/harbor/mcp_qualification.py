"""Verify native coding connections with separate setup credentials."""

from __future__ import annotations

import hashlib
import json

from examples.software_factory.factory.model import digest


def qualify(client, url, token, binding):
    endpoint = url.rstrip("/") + "/mcp?toolset=coding"
    headers = {
        "Authorization": "Bearer " + token,
        "X-Loom-Code-Scope": binding["org"] + "/" + binding["code_namespace"],
        "Accept": "application/json, text/event-stream",
    }

    def request(identifier, method, params):
        response = client.post(
            endpoint,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": identifier,
                "method": method,
                "params": params,
            },
        )
        response.raise_for_status()
        if response.headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        wire = response.json()
        if wire.get("error") or wire.get("id") != identifier:
            raise PermissionError("Native MCP qualification refused " + method)
        return wire["result"]

    try:
        initialized = request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "evaluation-qualification", "version": "1"},
            },
        )
        headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        response = client.post(
            endpoint,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        response.raise_for_status()
        tools = request(2, "tools/list", {})["tools"]
        names = {tool["name"] for tool in tools}
        required = {"loom_code_search", "loom_code_grep", "loom_code_symbol"}
        if not required <= names:
            raise PermissionError("Native coding tools are not advertised")
        result = request(
            3, "tools/call", {"name": "loom_code_grep", "arguments": {"pattern": "class Blueprint"}}
        )
        if result.get("isError") or "src/flask/blueprints.py" not in json.dumps(result):
            raise PermissionError("Native MCP code scope did not resolve the frozen repository")
        return {
            "qualified": True,
            "instructions_sha256": hashlib.sha256(
                initialized.get("instructions", "").encode("utf-8")
            ).hexdigest(),
            "instructions_chars": len(initialized.get("instructions", "")),
            "code_routes_named": sorted(
                name for name in required if name in initialized.get("instructions", "")
            ),
            "code_scope": {
                key: binding[key] for key in ("org", "code_namespace", "memory_namespace")
            },
            "tool_names": sorted(names),
            "tools_sha256": digest(tools),
            "provider_generation": False,
            "solver_credentials_used": False,
        }
    finally:
        if "Mcp-Session-Id" in headers:
            client.delete(endpoint, headers=headers).raise_for_status()
