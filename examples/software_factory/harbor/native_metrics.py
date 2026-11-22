"""Read native Harbor measurements without participating in agent execution."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

LOOM_TOOL_PREFIXES = ("mcp__loom__", "mcp__plugin_loom_loom__")


def is_loom_tool(name: str) -> bool:
    return name.startswith(LOOM_TOOL_PREFIXES)


def initialization(trial: Path) -> dict:
    result = dict.fromkeys(
        (
            "loom_connection_at_start",
            "loom_tools_advertised",
            "loom_plugin_loaded",
            "loom_code_skill_loaded",
            "loom_memory_skill_loaded",
        )
    )
    path = trial / "agent/claude-code.txt"
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "system" or row.get("subtype") != "init":
            continue
        mcp_servers = row.get("mcp_servers")
        if not isinstance(mcp_servers, list):
            mcp_servers = None
        servers = [
            server
            for server in mcp_servers or []
            if isinstance(server, dict)
            and server.get("name") in {"loom", "plugin:loom:loom", "plugin_loom_loom"}
        ]
        status = (
            servers[0].get("status")
            if len(servers) == 1
            else "disabled"
            if mcp_servers is not None and not servers
            else None
        )
        tools, plugins = row.get("tools"), row.get("plugins")
        if not isinstance(tools, list) or not all(isinstance(name, str) for name in tools):
            tools = None
        if not isinstance(plugins, list) or not all(isinstance(p, dict) for p in plugins):
            plugins = None
        result.update(
            loom_connection_at_start=status
            if status in {"connected", "pending", "failed", "disabled", "needs-auth"}
            else None,
            loom_tools_advertised=sum(is_loom_tool(name) for name in tools)
            if tools is not None
            else None,
            loom_plugin_loaded=any(p.get("name") == "loom" for p in plugins)
            if plugins is not None
            else None,
        )
        skills = row.get("skills")
        if not isinstance(skills, list) or not all(isinstance(name, str) for name in skills):
            skills = None
        names = {name.split(":")[-1] for name in skills} if skills is not None else None
        result.update(
            loom_code_skill_loaded="loom-code" in names if names is not None else None,
            loom_memory_skill_loaded="loom-memory" in names if names is not None else None,
        )
        return result
    return result


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def elapsed(value):
    if not isinstance(value, dict) or not value.get("started_at") or not value.get("finished_at"):
        return None
    return (
        datetime.fromisoformat(value["finished_at"]) - datetime.fromisoformat(value["started_at"])
    ).total_seconds()


def measurements(raw: dict, trial: Path) -> dict:
    """Harbor's Claude Code trajectory includes normalized subagent tool calls."""
    path = trial / "agent/trajectory.json"
    trajectory = json.loads(path.read_text()) if path.exists() else None
    if trajectory is not None and trajectory.get("agent", {}).get("name") != "claude-code":
        raise ValueError("Expected the native Harbor Claude Code trajectory")
    metrics = (trajectory or {}).get("final_metrics") or {}
    calls = None
    if trajectory is not None:
        calls = {}
        for step in trajectory.get("steps", []):
            if step.get("is_copied_context"):
                continue
            for call in step.get("tool_calls") or []:
                key = call["tool_call_id"]
                if key in calls and calls[key] != call:
                    raise ValueError("Native trajectory has conflicting tool call identifiers")
                calls[key] = call
    counts = (
        Counter(call["function_name"] for call in calls.values()) if calls is not None else None
    )
    commands = [
        call.get("arguments", {}).get("command", "")
        for call in (calls or {}).values()
        if call.get("function_name") == "Bash"
    ]
    test_commands = sum(
        bool(re.search(r"(?:^|[\s/;&|])(?:pytest|unittest)(?:\s|$)", cmd))
        for cmd in commands
        if isinstance(cmd, str)
    )
    incoming, outgoing = (
        number(metrics.get("total_prompt_tokens")),
        number(metrics.get("total_completion_tokens")),
    )
    extra = metrics.get("extra") or {}
    return {
        **initialization(trial),
        "measurement_source": "native Harbor Claude Code trajectory and trial timing",
        "solver": "claude-code",
        "solver_version": ((trajectory or {}).get("agent") or {}).get("version"),
        "generation_cost_usd": number(metrics.get("total_cost_usd")),
        "generation_cost_source": extra.get("cost_source", "claude-code-reported"),
        "model_input_tokens": incoming,
        "model_output_tokens": outgoing,
        "model_total_tokens": incoming + outgoing
        if incoming is not None and outgoing is not None
        else None,
        "model_cache_read_tokens": number(metrics.get("total_cached_tokens")),
        "model_cache_write_tokens": number(extra.get("total_cache_creation_input_tokens")),
        "tool_total_requests": len(calls) if calls is not None else None,
        "loom_tool_calls": sum(count for name, count in counts.items() if is_loom_tool(name))
        if counts is not None
        else None,
        "tool_counts": dict(counts) if counts is not None else None,
        "agent_test_runs": test_commands if calls is not None else None,
        "agent_test_runs_basis": (
            "Native Bash calls explicitly naming pytest or unittest; nested scripts unclassified"
        ),
        "memory_rows_written": None,
        "agent_time_s": elapsed(raw.get("agent_execution")),
        "end_to_end_s": elapsed(raw),
        "harbor_wall_s": elapsed(raw),
    }
