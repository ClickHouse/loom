"""Verify native coding connections with separate setup credentials."""

from __future__ import annotations

import hashlib
import json

from examples.software_factory.factory.model import digest

from .native_environment import MCP_SCHEMA_PROFILES, mcp_endpoint
from .scopes import frozen_json


def qualify(client, url, token, binding, *, schema_profile):
    endpoint = mcp_endpoint(url, schema_profile)
    headers = {
        "Authorization": "Bearer " + token,
        "X-Loom-Code-Scope": binding["org"] + "/" + binding["code_namespace"],
        "Accept": "application/json, text/event-stream",
        "Connection": "close",
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
            "tool_loading_sha256": digest(
                {
                    tool["name"]: bool(tool.get("_meta", {}).get("anthropic/alwaysLoad"))
                    for tool in tools
                }
            ),
            "tools_sha256": digest(tools),
            "provider_generation": False,
            "solver_credentials_used": False,
        }
    finally:
        if "Mcp-Session-Id" in headers:
            client.delete(endpoint, headers=headers).raise_for_status()


def qualified_catalog(schema_profile, receipts, standard):
    """Confirm one catalog across the Loom arms and a real compact projection."""
    if schema_profile not in MCP_SCHEMA_PROFILES:
        raise PermissionError("Unrecognized Loom MCP schema profile")
    catalogs = {row["tools_sha256"] for row in receipts.values()}
    if len(catalogs) != 1:
        raise PermissionError("Loom arm catalogs differ")
    (catalog,) = catalogs
    if schema_profile == "compact":
        sample = next(iter(receipts.values()))
        if (
            standard is None
            or standard["tools_sha256"] == catalog
            or standard["tool_names"] != sample["tool_names"]
            or standard["tool_loading_sha256"] != sample["tool_loading_sha256"]
        ):
            raise PermissionError("Compact catalog must preserve the tools and loading policy")
    return {
        "schema_profile": schema_profile,
        "tool_catalog_sha256": catalog,
        "standard_catalog_sha256": standard["tools_sha256"] if standard else catalog,
    }


def verify_catalog(protocol):
    """Check execution and reporting against the same immutable receipts."""
    execution = protocol.get("loom_mcp")
    public = protocol.get("public_reproduction", {}).get("loom_mcp")
    path = protocol.get("mcp_qualification_file")
    if execution is None and public is None and path is None:
        return
    if not path:
        raise PermissionError("Loom MCP catalog qualification is missing")
    receipt = frozen_json(path, protocol["public_inputs"].get(path))
    expected_orders = {
        str(slot["order"]) for slot in protocol["schedule"] if slot["mode"] != "baseline"
    }
    if set(receipt["attempts"]) != expected_orders:
        raise PermissionError("Loom MCP qualification does not cover every Loom attempt")
    catalog = qualified_catalog(
        receipt["schema_profile"], receipt["attempts"], receipt["standard_control"]
    )
    if catalog != execution or catalog != public:
        raise PermissionError("Loom MCP profile differs from the qualified catalog")
