"""Read native Harbor measurements without participating in agent execution."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path


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
        "loom_tool_calls": sum(
            count for name, count in counts.items() if name.startswith("mcp__loom__")
        )
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
