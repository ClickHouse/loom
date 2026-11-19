"""Task identity, tool contracts and append-only evidence."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MODES = ("baseline", "orchestrated", "natural")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str
    repository: str
    base_commit: str
    namespace: str
    problem: str
    symbol: str
    files: dict[str, str]
    source_sha256: str

    @classmethod
    def load(cls, path: Path) -> Task:
        task = cls(**json.loads(path.read_text()))
        if task.kind != "authored-expression-fixture":
            raise ValueError("Historical tasks require a qualified sandbox runner")
        if digest(task.files) != task.source_sha256:
            raise ValueError("Frozen source hash mismatch")
        if not task.namespace or not task.repository or not task.base_commit:
            raise ValueError("Task scope is incomplete")
        return task


@dataclass(frozen=True)
class Budget:
    turns: int = 18
    tools: int = 30
    retrievals: int = 6
    context_chars: int = 12000
    transcript_chars: int = 64000
    output_tokens: int = 2048
    wall_seconds: float = 60

    def __post_init__(self) -> None:
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("Budgets must be positive")


class Provider(Protocol):
    label: str

    def step(self, messages: list[dict], tools: list[dict], max_tokens: int) -> dict: ...


class Retrieval(Protocol):
    label: str

    def request(self, op: str, body: dict) -> dict: ...


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.started = time.monotonic()
        self.rows: list[dict] = []

    def emit(self, kind: str, **fields: Any) -> None:
        row = {
            "sequence": len(self.rows),
            "elapsed_s": time.monotonic() - self.started,
            "kind": kind,
            **fields,
        }
        row = json.loads(json.dumps(row))
        self.rows.append(row)
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def tool_schemas(mode: str) -> list[dict]:
    string = {"type": "string"}
    tools = [
        tool("read", "Read one source file in the task checkout.", {"path": string}),
        tool("search", "Find a literal in allowed source files.", {"text": string}),
        tool(
            "edit",
            "Replace exactly one nonempty source fragment.",
            {"path": string, "old": string, "new": string},
        ),
        tool("test", "Run fixed visible objective checks.", {}),
    ]
    if mode == "natural":
        tools.append(
            tool(
                "loom",
                "Optional exact definition lookup, verified against local source.",
                {"symbol": string},
            )
        )
    return tools
