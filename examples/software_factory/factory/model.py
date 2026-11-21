"""Task identity, tool contracts and append-only evidence."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MODES = ("baseline", "natural")


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


@dataclass(frozen=True)
class Budget:
    turns: int | None = 18
    tools: int | None = 30
    retrievals: int | None = 6
    context_chars: int | None = 12000
    transcript_chars: int | None = 64000
    output_tokens: int = 2048
    wall_seconds: float | None = 60
    response_chars: int | None = 24000

    def __post_init__(self) -> None:
        if any(value is not None and value <= 0 for value in vars(self).values()):
            raise ValueError("Budgets must be positive")


class TestRefusal(ValueError):
    """A grader refused before starting a test process."""


class Grader(Protocol):
    sha256: str

    def run(self, checkout: Any, *, hidden: bool = False) -> dict: ...


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


def tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False,
        },
    }


def tool_schemas(
    mode: str, *, regression_tests: bool = False, project_notes: bool = False, memory: bool = False
) -> list[dict]:
    string = {"type": "string"}
    tools = [
        tool(
            "read",
            "Read a source page. Continue truncated reads with "
            "next_start_line and next_start_column.",
            {
                "path": string,
                "start_line": {"type": "integer", "minimum": 1},
                "start_column": {"type": "integer", "minimum": 0},
            },
            ["path"],
        ),
        tool(
            "search",
            "Find a literal in allowed source files; follow next_offset for more matches.",
            {"text": string, "offset": {"type": "integer", "minimum": 0}},
            ["text"],
        ),
        tool(
            "edit",
            "Replace exactly one nonempty source fragment.",
            {"path": string, "old": string, "new": string},
        ),
        tool("test", "Run fixed available tests; passing does not verify task completion.", {}),
    ]
    if regression_tests:
        tools.append(
            tool(
                "regression_test",
                "Run a self-contained regression test against current source and inspect "
                "its failure detail. Evaluator test fixtures are unavailable to this tool.",
                {"source": {"type": "string", "minLength": 1, "maxLength": 16000}},
            )
        )
    if project_notes:
        from examples.software_factory.factory.project_notes import schemas as note_schemas

        tools.extend(note_schemas())
    if mode == "natural":
        from examples.software_factory.factory.retrieval_tools import schemas

        tools.extend(schemas())
    if memory:
        if mode != "natural":
            raise ValueError("Native memory requires the optional Loom arm")
        from examples.software_factory.factory.memory_tools import schemas as memory_schemas

        tools.extend(memory_schemas())
    return tools
