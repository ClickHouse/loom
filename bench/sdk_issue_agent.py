"""Identical local issue tools and a conditional Loom SDK binding for both arms."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

POLICY = (
    "Fix the issue using the supplied tools and leave the proposed changes in the checkout. "
    "Inspect the current implementation before editing. When an indexed lookup tool is "
    "available, use it first for definitions, callers, proposed effects and conceptual code "
    "questions. Use local tools for exact literals, untracked or modified files, or when "
    "the index abstains or cannot answer. Reuse evidence already returned; do not retrieve "
    "for a self-contained question. Never make an unrelated call just to use a tool. "
    "Static caller and impact graphs are lower bounds, not exhaustive runtime evidence. "
    "All tool paths are relative to this checkout. Do not access parent directories, .git, "
    "history, network resources or credentials. Do not execute code or tests. The same "
    "selected tests run externally for both arms. Stop on a permission or guard refusal. "
    "Finish with a short account of the changes and limitations."
)


class ToolError(ValueError):
    """An input error whose message contains no transport credentials."""


def tool_spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "toolSpec": {
            "name": name,
            "description": description,
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                }
            },
        }
    }


def tools(arm: str) -> list[dict]:
    text = {"type": "string"}
    local = [
        tool_spec(
            "search_code",
            "Search checkout text by regex with bounded optional surrounding context; "
            "paths and matches are relative and total output is capped.",
            {
                "pattern": text,
                "path": text,
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 20},
            },
            ["pattern"],
        ),
        tool_spec(
            "read_file",
            "Read at most 150 lines from a relative checkout file.",
            {
                "path": text,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
        tool_spec(
            "replace_text",
            "Replace one exact unique match in a relative file. To create "
            "a missing file, use an empty old string; it cannot overwrite an existing file.",
            {"path": text, "old": text, "new": text},
            ["path", "old", "new"],
        ),
    ]
    if arm == "loom":
        local.insert(
            0,
            tool_spec(
                "loom_code_lookup",
                "Read the indexed definition, static callers, "
                "concept search or prospective impact for the supplied target. Reuse returned "
                "evidence and honor ambiguity, abstention and partial-graph warnings.",
                {
                    "kind": {"type": "string", "enum": ["symbol", "callers", "search", "impact"]},
                    "target": text,
                },
                ["kind", "target"],
            ),
        )
    return local


def local_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if (
        relative.is_absolute()
        or "\\" in value
        or any(p == ".." or p.lower() == ".git" for p in relative.parts)
    ):
        raise PermissionError("Only relative checkout paths outside .git are allowed")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or any(
        p.lower() == ".git" for p in path.relative_to(root).parts
    ):
        raise PermissionError("Resolved path leaves the allowed checkout")
    return path


def local_tool(root: Path, name: str, values: dict) -> dict:
    path = local_path(root, values.get("path", "."))
    relative = str(path.relative_to(root))
    if name == "search_code":
        context = int(values.get("context_lines", 0))
        if not 0 <= context <= 20:
            raise ToolError("Search context_lines must be between 0 and 20")
        proc = subprocess.run(
            [
                "rg",
                "-n",
                "--with-filename",
                "-C",
                str(context),
                "--color",
                "never",
                "--max-count",
                "12",
                "--max-columns",
                "300",
                "--glob",
                "!.git",
                "--glob",
                "!**/.git/**",
                "--",
                values["pattern"],
                relative,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode not in (0, 1):
            raise ToolError("Source search failed")
        return {"matches": proc.stdout[:10000], "truncated": len(proc.stdout) > 10000}
    if name == "read_file":
        lines = path.read_text().splitlines()
        start = max(1, int(values.get("start_line", 1)))
        end = min(int(values.get("end_line", start + 99)), start + 149, len(lines))
        content = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return {
            "path": relative,
            "total_lines": len(lines),
            "content": content[:18000],
            "truncated": len(content) > 18000,
        }
    if name != "replace_text":
        raise ToolError("Unknown local tool")
    old, new = values["old"], values["new"]
    created = not path.exists()
    contents = "" if created else path.read_text()
    if (created and old) or (not created and (not old or contents.count(old) != 1)):
        raise ToolError("Old text must match exactly once; empty old is only for a missing file")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new if created else contents.replace(old, new, 1))
    return {"path": relative, "updated": True, "created": created}


def lookup_body(kind: str, target: str, manifest: dict) -> dict:
    body = {"org": manifest["scope"]["org"], "namespace": manifest["scope"]["namespace"]}
    if kind == "symbol":
        body.update(
            name=target,
            include_body=True,
            include_history=False,
            include_examples=False,
            include_lessons=False,
            token_budget=1200,
        )
    elif kind == "callers":
        body.update(name=target, depth=1, max_nodes=20, include_lessons=False)
    elif kind == "search":
        body.update(query=target, top_k=3, token_budget=1200, include_lessons=False)
    elif kind == "impact":
        body.update(name=target, depth=1, per_hop_limit=20, include_lessons=False)
    else:
        raise ToolError("Unknown lookup kind")
    return body


def error_detail(error: BaseException) -> dict:
    # Transport exceptions can include request headers; only harness errors carry text.
    return {
        "type": type(error).__name__,
        "message": str(error)[:2000] if isinstance(error, ToolError) else "Operation failed",
        "code": getattr(error, "code", ""),
        "http_status": getattr(error, "status", None),
        "request_id": getattr(error, "request_id", ""),
    }


def guard_refused(error: BaseException) -> bool:
    return isinstance(error, PermissionError) or getattr(error, "status", 0) in (401, 403, 409)


async def dispatch(root: Path, request: dict, sdk: Any, manifest: dict, record: Any) -> dict:
    name, values = request["name"], request["input"]
    if name != "loom_code_lookup":
        return local_tool(root, name, values)
    if sdk is None:
        raise PermissionError("Indexed lookup is not available in this arm")
    kind = values["kind"]
    result = await sdk.call_with_meta("code." + kind, lookup_body(kind, values["target"], manifest))
    record(
        {
            "type": "loom_sdk_result",
            "operation": "code." + kind,
            "request_id": result.request_id,
            "data": result.data,
        }
    )
    return result.data


async def perform(root: Path, request: dict, sdk: Any, manifest: dict, record: Any) -> dict:
    started = time.monotonic()
    record(
        {
            "type": "tool_attempt",
            "name": request["name"],
            "input": request["input"],
            "tool_use_id": request["toolUseId"],
        }
    )
    try:
        data = await dispatch(root, request, sdk, manifest, record)
        result = {
            "toolUseId": request["toolUseId"],
            "content": [{"json": data}],
            "status": "success",
        }
    except Exception as error:  # Record failed calls before a refusal stops the worker.
        detail = error_detail(error)
        record(
            {
                "type": "tool_result",
                "name": request["name"],
                "input": request["input"],
                "status": "error",
                "error": detail,
                "duration_s": time.monotonic() - started,
            }
        )
        if guard_refused(error):
            raise
    else:
        record(
            {
                "type": "tool_result",
                "name": request["name"],
                "input": request["input"],
                "status": "success",
                "result": result,
                "duration_s": time.monotonic() - started,
            }
        )
        return result
    return {"toolUseId": request["toolUseId"], "content": [{"json": detail}], "status": "error"}


async def agent_loop(root: Path, manifest: dict, client: Any, sdk: Any, record: Any) -> dict:
    messages = [{"role": "user", "content": [{"text": manifest["task"]["problem_statement"]}]}]
    for turn in range(manifest["protocol"]["max_turns"]):
        response = client.converse(
            modelId=manifest["protocol"]["model"],
            system=[{"text": manifest["protocol"]["system"]}],
            messages=messages,
            toolConfig={"tools": manifest["tools"], "toolChoice": {"auto": {}}},
            inferenceConfig=manifest["protocol"]["inference_config"],
        )
        record({"type": "model_response", "turn": turn, "response": response})
        message = response["output"]["message"]
        messages.append(message)
        requests = [p["toolUse"] for p in message["content"] if "toolUse" in p]
        if not requests:
            reason = response.get("stopReason")
            return {
                "status": "completed" if reason == "end_turn" else "model_limit",
                "stop_reason": reason,
                "answer": "\n".join(p.get("text", "") for p in message["content"]),
            }
        results = [await perform(root, request, sdk, manifest, record) for request in requests]
        messages.append({"role": "user", "content": [{"toolResult": r} for r in results]})
    return {"status": "max_turns", "answer": ""}


def append_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time_unix": time.time(), **event}, default=str) + "\n")
