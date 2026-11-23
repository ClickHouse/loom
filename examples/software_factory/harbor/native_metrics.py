"""Read native Harbor measurements without participating in agent execution."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .pricing import first_prompt_input_prices
from .runtime import write

LOOM_TOOL_PREFIXES = ("mcp__loom__", "mcp__plugin_loom_loom__")
logger = logging.getLogger(__name__)


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


def uncached_input(total, cache_reads, cache_writes):
    if all(type(value) is int and value >= 0 for value in (total, cache_reads, cache_writes)):
        return number(total - cache_reads - cache_writes)
    return None


def cache_usage(extra):
    reads = number(extra.get("cache_read_input_tokens"))
    writes = number(extra.get("cache_creation_input_tokens"))
    partition = extra.get("cache_creation")
    partition = partition if isinstance(partition, dict) else {}
    short_writes = number(partition.get("ephemeral_5m_input_tokens"))
    long_writes = number(partition.get("ephemeral_1h_input_tokens"))
    if writes == 0 and not partition:
        short_writes = long_writes = 0
    if short_writes is None or long_writes is None or short_writes + long_writes != writes:
        short_writes = long_writes = None
    return {
        "cache_read_tokens": reads,
        "cache_write_tokens": writes,
        "cache_write_5m_tokens": short_writes,
        "cache_write_1h_tokens": long_writes,
    }


def reported_usage(trial: Path):
    """Read the provider counters recorded by Claude Code before Harbor normalizes them."""
    path = trial / "agent/claude-code.txt"
    totals, first = {}, {}
    first_id = None
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "result":
                usage = event.get("usage")
                totals = usage if isinstance(usage, dict) else {}
            if (
                event.get("type") == "assistant"
                and event.get("parent_tool_use_id") is None
                and not event.get("isSidechain")
            ):
                message = event.get("message") or {}
                if not isinstance(message, dict):
                    continue
                identifier = message.get("id")
                if isinstance(identifier, str) and first_id is None:
                    first_id = identifier
                usage = message.get("usage")
                if first_id is not None and identifier == first_id and isinstance(usage, dict):
                    first.update(usage)
    return totals, first


def reported_input(usage, cache):
    pieces = [usage.get("input_tokens"), cache["cache_read_tokens"], cache["cache_write_tokens"]]
    return sum(pieces) if all(type(piece) is int and piece >= 0 for piece in pieces) else None


def elapsed(value):
    if not isinstance(value, dict) or not value.get("started_at") or not value.get("finished_at"):
        return None
    return (
        datetime.fromisoformat(value["finished_at"]) - datetime.fromisoformat(value["started_at"])
    ).total_seconds()


def capture_turn_usage(trial: Path, target: Path) -> None:
    """Save numeric native step usage beside the private tool trace."""
    try:
        raw = (trial / "agent/trajectory.json").read_bytes()
        trajectory = json.loads(raw)
        if trajectory.get("agent", {}).get("name") != "claude-code":
            raise ValueError("Expected the native Harbor Claude Code trajectory")
        turns = []
        for step in trajectory.get("steps", []):
            metrics = step.get("metrics")
            if not metrics or step.get("is_copied_context"):
                continue
            input_tokens = number(metrics.get("prompt_tokens"))
            cache = cache_usage(metrics.get("extra") or {})
            turns.append(
                {
                    "step_id": step["step_id"],
                    "tool_call_ids": [
                        call["tool_call_id"] for call in step.get("tool_calls") or []
                    ],
                    "input_tokens": input_tokens,
                    "uncached_input_tokens": uncached_input(
                        input_tokens, cache["cache_read_tokens"], cache["cache_write_tokens"]
                    ),
                    "output_tokens": number(metrics.get("completion_tokens")),
                    **cache,
                    "estimated_cost_usd": number(metrics.get("cost_usd")),
                }
            )
        write(target, {"trajectory_sha256": hashlib.sha256(raw).hexdigest(), "turns": turns})
    except Exception as error:  # noqa: BLE001 — optional diagnostics cannot sink a run
        logger.warning("Model usage capture failed for %s (%s)", target.name, type(error).__name__)


def measurements(raw: dict, trial: Path) -> dict:
    """Harbor's Claude Code trajectory includes normalized subagent tool calls."""
    from .native_trace import summarize

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
    extra = metrics.get("extra") or {}
    provider, first_provider = reported_usage(trial)
    cache = cache_usage(provider)
    incoming = reported_input(provider, cache)
    outgoing = number(provider.get("output_tokens"))
    steps = (trajectory or {}).get("steps", [])
    first_step = next(
        (
            step
            for step in steps
            if step.get("source") == "agent"
            and not step.get("is_copied_context")
            and not (step.get("extra") or {}).get("is_sidechain")
        ),
        {},
    )
    first = first_step.get("metrics") or {}
    first_extra = first.get("extra") or {}
    first_cache = cache_usage(first_provider)
    first_input = reported_input(first_provider, first_cache)
    first_read = first_cache["cache_read_tokens"]
    first_write = first_cache["cache_write_tokens"]
    first_prices = {}
    if (
        first_extra.get("service_tier", "standard") == "standard"
        and first_extra.get("inference_geo", "global") == "global"
    ):
        first_prices = first_prompt_input_prices(
            first_step.get("model_name"),
            prompt_tokens=first_input,
            output_tokens=first.get("completion_tokens"),
            cached_tokens=first_read,
            written_tokens=first_write,
            long_writes=(first_extra.get("cache_creation") or {}).get(
                "ephemeral_1h_input_tokens", 0
            ),
            step_cost=number(first.get("cost_usd")),
        )
    return {
        **initialization(trial),
        **summarize(trial)["metrics"],
        "measurement_source": "native Harbor Claude Code trajectory and trial timing",
        "solver": "claude-code",
        "solver_version": ((trajectory or {}).get("agent") or {}).get("version"),
        "generation_cost_usd": number(metrics.get("total_cost_usd")),
        "generation_cost_source": extra.get("cost_source", "claude-code-reported"),
        "solver_token_source": "anthropic-reported" if provider else None,
        "model_input_tokens": incoming,
        "model_output_tokens": outgoing,
        "model_total_tokens": incoming + outgoing
        if incoming is not None and outgoing is not None
        else None,
        **{"model_" + key: value for key, value in cache.items()},
        "model_uncached_input_tokens": number(provider.get("input_tokens")),
        "first_cache_read_tokens": first_read,
        "first_cache_write_tokens": first_write,
        "first_prompt_input_tokens": first_input,
        "first_uncached_input_tokens": number(first_provider.get("input_tokens")),
        **first_prices,
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
