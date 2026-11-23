"""Summarize recorded main-session tools without executing or retaining their text."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter

COUNTERS = (
    "model_turns_main",
    "parallel_tool_groups_main",
    "tool_discovery_calls_main",
    "tool_calls_main",
    "loom_calls_main",
    "loom_errors_main",
    "tool_result_chars_main",
    "loom_result_chars_main",
    "loom_source_chars_main",
    "loom_lesson_chars_main",
    "loom_repeated_lesson_chars_main",
)
TIMINGS = ("claude_cli_time_s", "claude_api_time_s")


def _body(content):
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _content_counts(body, seen):
    rows = [body]
    for key in ("results", "definitions"):
        if isinstance(values := body.get(key), list):
            rows.extend(value for value in values if isinstance(value, dict))
    lessons = [
        lesson
        for row in rows
        if isinstance(row.get("lessons"), list)
        for lesson in row["lessons"]
        if isinstance(lesson, dict)
    ]
    repeated, states = 0, Counter()
    for lesson in lessons:
        state = lesson.get("code_state")
        states[
            state
            if state in {"fresh", "moved", "changed", "unresolved", "missing"}
            else "unclassified"
        ] += 1
        key = lesson.get("namespace"), lesson.get("memory_key")
        excerpt = lesson.get("excerpt")
        if all(isinstance(part, str) and part for part in key):
            identity = (*key, json.dumps(lesson, sort_keys=True, separators=(",", ":")))
            if identity in seen and isinstance(excerpt, str):
                repeated += len(excerpt)
            seen.add(identity)
    return {
        "source_chars": sum(len(row["body"]) for row in rows if isinstance(row.get("body"), str)),
        "source_bodies": sum(
            bool(row.get("body")) for row in rows if isinstance(row.get("body"), str)
        ),
        "elided_bodies": sum(row.get("body_elided") is True for row in rows),
        "lesson_rows": len(lessons),
        "lesson_chars": sum(
            len(row["excerpt"]) for row in lessons if isinstance(row.get("excerpt"), str)
        ),
        "repeated_lesson_chars": repeated,
        "lesson_states": dict(states),
    }


def summarize(trial):
    path = trial / "agent/claude-code.txt"
    summary = {key: None for key in (*COUNTERS, *TIMINGS)}
    output = {
        "metrics": summary,
        "calls": [],
        "source_sha256": None,
        "scope": (
            "Main CLI stream only; native Harbor trajectory remains authoritative "
            "for total tools and subagents."
        ),
    }
    if not path.exists():
        return output
    raw = path.read_bytes()
    output["source_sha256"] = hashlib.sha256(raw).hexdigest()
    calls, responses, replies, final = {}, set(), {}, None
    for line in raw.decode().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            final = event
        message = event.get("message") or {}
        if not isinstance(message, dict):
            continue
        response_id = message.get("id")
        if event.get("type") == "assistant" and isinstance(response_id, str):
            responses.add(response_id)
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[block["id"]] = {
                    "tool_call_id": block["id"],
                    "tool": block["name"],
                    "model_response_id": response_id,
                }
            elif block.get("type") == "tool_result":
                replies[block["tool_use_id"]] = block
    summary.update(dict.fromkeys(COUNTERS, 0))
    summary["model_turns_main"] = len(responses)
    groups, seen = Counter(), set()
    for identifier, call in calls.items():
        name = call["tool"]
        loom = name.startswith(("mcp__loom__", "mcp__plugin_loom_loom__"))
        reply = replies.get(identifier)
        content = reply.get("content") if reply else None
        chars = (
            len(content)
            if isinstance(content, str)
            else len(json.dumps(content, ensure_ascii=False))
            if content is not None
            else None
        )
        observed = {
            **call,
            "result_observed": reply is not None,
            "is_error": bool(reply.get("is_error", False)) if reply else None,
            "result_chars": chars,
        }
        groups[call["model_response_id"]] += 1
        summary["tool_result_chars_main"] += chars or 0
        summary["tool_discovery_calls_main"] += name == "ToolSearch"
        if loom:
            summary["loom_calls_main"] += 1
            summary["loom_errors_main"] += observed["is_error"] is True
            summary["loom_result_chars_main"] += chars or 0
            body = _body(content)
            if body is not None:
                counted = _content_counts(body, seen)
                observed.update(counted)
                for source, target in (
                    ("source_chars", "loom_source_chars_main"),
                    ("lesson_chars", "loom_lesson_chars_main"),
                    ("repeated_lesson_chars", "loom_repeated_lesson_chars_main"),
                ):
                    summary[target] += counted[source]
                trace_id = body.get("trace_run_id")
                if isinstance(trace_id, str) and len(trace_id) <= 128:
                    observed["trace_run_id"] = trace_id
        output["calls"].append(observed)
    summary["tool_calls_main"] = len(calls)
    summary["parallel_tool_groups_main"] = sum(size > 1 for size in groups.values())
    for source, target in (
        ("duration_ms", "claude_cli_time_s"),
        ("duration_api_ms", "claude_api_time_s"),
    ):
        value = (final or {}).get(source)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            summary[target] = value / 1000
    return output
