"""Summarize recorded main-session tools without executing or retaining their text."""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from collections import Counter

SOURCE_COUNTERS = (
    ("source_chars", "loom_source_chars_main"),
    ("match_chars", "loom_match_chars_main"),
    ("context_chars", "loom_context_chars_main"),
    ("caller_context_chars", "loom_caller_context_chars_main"),
)
LESSON_COUNTERS = (
    ("lesson_chars", "loom_lesson_chars_main"),
    ("repeated_lesson_chars", "loom_repeated_lesson_chars_main"),
    ("fresh_linked_lesson_rows", "loom_fresh_linked_lesson_rows_main"),
    ("nonfresh_linked_lesson_rows", "loom_nonfresh_linked_lesson_rows_main"),
)
CONTENT_COUNTERS = SOURCE_COUNTERS + LESSON_COUNTERS
COUNTERS = (
    "model_turns_main",
    "parallel_tool_groups_main",
    "tool_discovery_calls_main",
    "tool_calls_main",
    "loom_calls_main",
    "loom_errors_main",
    "tool_result_chars_main",
    "loom_result_chars_main",
    *(target for _, target in CONTENT_COUNTERS),
)
EVENTS = (
    "first_fresh_linked_lesson_result_event_main",
    "first_possible_edit_request_event_main",
    "first_loom_source_result_event_main",
    "first_indexed_read_result_event_main",
)
POSSIBLE_EDIT_TOOLS = {"agent", "bash", "edit", "multiedit", "notebookedit", "task", "write"}
NONFRESH_LINK_STATES = {"moved", "changed", "unresolved", "missing"}
TIMINGS = ("claude_cli_time_s", "claude_api_time_s")


def _body(content):
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _dict_rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text_chars(rows, key):
    return sum(len(text) for row in rows if isinstance(text := row.get(key), str))


def _content_counts(body, seen):
    rows = [body]
    for key in ("results", "definitions"):
        rows.extend(_dict_rows(body.get(key)))
    lessons = [lesson for row in rows for lesson in _dict_rows(row.get("lessons"))]
    caller_contexts = [
        context
        for row in rows
        for example in _dict_rows(row.get("examples"))
        if isinstance(context := example.get("context"), dict)
    ]
    repeated, states, usable_states = 0, Counter(), Counter()
    for lesson in lessons:
        state = lesson.get("code_state")
        states[state if state == "fresh" or state in NONFRESH_LINK_STATES else "unclassified"] += 1
        key = lesson.get("namespace"), lesson.get("memory_key")
        excerpt = lesson.get("excerpt")
        if isinstance(excerpt, str) and excerpt.strip() and isinstance(state, str):
            usable_states[state] += 1
        if (
            isinstance(excerpt, str)
            and excerpt
            and all(isinstance(part, str) and part for part in key)
        ):
            identity = (*key, " ".join(excerpt.split()))
            if identity in seen:
                repeated += len(excerpt)
            seen.add(identity)
    return {
        "source_chars": _text_chars(rows, "body")
        + sum(_text_chars(_dict_rows(row.get("windows")), "text") for row in rows),
        "match_chars": _text_chars(_dict_rows(body.get("matches")), "text"),
        "context_chars": _text_chars(_dict_rows(body.get("contexts")), "text"),
        "caller_context_chars": _text_chars(caller_contexts, "text"),
        "source_bodies": sum(
            bool(row.get("body")) for row in rows if isinstance(row.get("body"), str)
        ),
        "elided_bodies": sum(row.get("body_elided") is True for row in rows),
        "lesson_rows": len(lessons),
        "lesson_chars": _text_chars(lessons, "excerpt"),
        "repeated_lesson_chars": repeated,
        "lesson_states": dict(states),
        "fresh_linked_lesson_rows": usable_states["fresh"],
        "nonfresh_linked_lesson_rows": sum(usable_states[state] for state in NONFRESH_LINK_STATES),
    }


def _indexed_read(block, roots):
    if block.get("name") != "Read" or not roots:
        return False
    arguments = block.get("input")
    raw = arguments.get("file_path") if isinstance(arguments, dict) else None
    if not isinstance(raw, str):
        return False
    path = posixpath.normpath(posixpath.join("/workspace", raw))
    return any(path.startswith("/workspace/" + root.strip("/") + "/") for root in roots)


def summarize(trial, *, source_roots=()):
    path = trial / "agent/claude-code.txt"
    summary = dict.fromkeys((*COUNTERS, *EVENTS, *TIMINGS))
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
    indexed_read_ids = set()
    for event_index, line in enumerate(raw.decode().splitlines(), 1):
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
                if _indexed_read(block, source_roots):
                    indexed_read_ids.add(block["id"])
                calls.setdefault(
                    block["id"],
                    {
                        "tool_call_id": block["id"],
                        "tool": block["name"],
                        "model_response_id": response_id,
                        "request_event": event_index,
                    },
                )
            elif block.get("type") == "tool_result":
                replies.setdefault(block["tool_use_id"], (event_index, block))
    summary.update(dict.fromkeys(COUNTERS, 0))
    summary["model_turns_main"] = len(responses)
    groups, seen, fresh_events, source_events = Counter(), set(), [], []
    summary["first_indexed_read_result_event_main"] = min(
        (
            replies[identifier][0]
            for identifier in indexed_read_ids
            if identifier in replies and not replies[identifier][1].get("is_error", False)
        ),
        default=None,
    )
    content_counts_complete = True
    summary["first_possible_edit_request_event_main"] = next(
        (
            call["request_event"]
            for call in calls.values()
            if call["tool"].casefold() in POSSIBLE_EDIT_TOOLS
        ),
        None,
    )
    for identifier, call in calls.items():
        name = call["tool"]
        loom = name.startswith(("mcp__loom__", "mcp__plugin_loom_loom__"))
        result_event, reply = replies.get(identifier, (None, None))
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
            "result_event": result_event,
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
            if not observed["is_error"] and body is None:
                content_counts_complete = False
            if body is not None and not observed["is_error"]:
                counted = _content_counts(body, seen)
                if counted["fresh_linked_lesson_rows"]:
                    fresh_events.append(result_event)
                if "loom_code_" in name and any(counted[key] > 0 for key, _ in SOURCE_COUNTERS):
                    source_events.append(result_event)
                observed.update(counted)
                for source, target in CONTENT_COUNTERS:
                    summary[target] += counted[source]
                trace_id = body.get("trace_run_id")
                if isinstance(trace_id, str) and len(trace_id) <= 128:
                    observed["trace_run_id"] = trace_id
        output["calls"].append(observed)
    if not content_counts_complete:
        for _, target in CONTENT_COUNTERS:
            summary[target] = None
    else:
        summary["first_fresh_linked_lesson_result_event_main"] = min(fresh_events, default=None)
    summary["first_loom_source_result_event_main"] = min(source_events, default=None)
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
