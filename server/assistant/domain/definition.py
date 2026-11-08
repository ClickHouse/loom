"""The assistant's agent definition, as `integrations/librechat/agent.yaml` holds it,
and the rule for when a boot writes it into LibreChat.

A boot writes the definition when its version is newer than the stored one, or the
same with another model, so during a rolling upgrade a build N-1 never overwrites N's
definition and a changed `LOOM_ASSISTANT_MODEL` takes effect at the next boot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# LibreChat names an MCP server's tool `<tool><delimiter><server>`.
MCP_DELIMITER = "_mcp_"
MCP_SERVER = "loom"


@dataclass(frozen=True)
class Definition:
    name: str
    version: int
    provider: str
    model: str
    instructions: str
    tools: tuple[str, ...]

    def agent_body(self) -> dict[str, Any]:
        """The body LibreChat's agents endpoint takes to create or update it."""
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "model_parameters": {"thinking": False},
            "instructions": self.instructions,
            "tools": list(self.tools),
        }


def parse(raw: dict[str, Any], tool_names: list[str], model: str = "") -> Definition:
    """The definition from agent.yaml's fields, its tools from the assistant toolset's
    MCP tool names, and `model` (LOOM_ASSISTANT_MODEL) in place of the file's when set."""
    for field in ("name", "version", "provider", "model", "instructions"):
        if field not in raw:
            raise ValueError(f"agent.yaml has no {field!r}")
    return Definition(
        name=str(raw["name"]),
        version=int(raw["version"]),
        provider=str(raw["provider"]),
        model=model or str(raw["model"]),
        instructions=str(raw["instructions"]).strip(),
        tools=tuple(f"{t}{MCP_DELIMITER}{MCP_SERVER}" for t in sorted(tool_names)),
    )


def needs_write(stored_version: int, stored_model: str, definition: Definition) -> bool:
    """Whether a boot writes `definition` over the one stored at `stored_version` with
    `stored_model` (0 and '' for none)."""
    if definition.version != stored_version:
        return definition.version > stored_version
    return definition.model != stored_model
