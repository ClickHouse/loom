"""The `tasks` MCP toolset (wake-up layer): the task inbox
plane's roster, listed under `?toolset=tasks` and nowhere else by default.
Names task.pull, task.create, task.complete, task.fail, task.wont_do,
task.defer, task.heartbeat, task.updates, task.list, task.get, inbox.list."""

from __future__ import annotations

import asyncio
import json

from server.operations.toolsets import (
    _MCP_CORE_OPS,
    _MCP_TASK_OPS,
    CURATED_TOOLSETS,
    MCP_TOOLSETS,
    curated_roster,
    op_toolsets,
    resolve_toolset,
)
from server.transport.mcp_tools import _tools_descriptor

TASK_OPS = {
    "task.pull",
    "task.create",
    "task.complete",
    "task.fail",
    "task.wont_do",
    "task.defer",
    "task.heartbeat",
    "task.updates",
    "task.list",
    "task.get",
    "inbox.list",
}


def _listing(toolset: str) -> list[dict]:
    return asyncio.run(_tools_descriptor(toolset=toolset))


def test_tasks_is_a_curated_roster_of_its_own_and_core_is_untouched() -> None:
    assert _MCP_TASK_OPS == TASK_OPS
    assert "tasks" in MCP_TOOLSETS and "tasks" in CURATED_TOOLSETS
    assert curated_roster("tasks", code_tools=True) == _MCP_TASK_OPS
    assert not (_MCP_CORE_OPS & _MCP_TASK_OPS), "the default listing does not pay for the plane"
    assert resolve_toolset("tasks", "core") == "tasks"


def test_tasks_listing_is_the_roster_plus_the_synthetic_tools() -> None:
    tools = _listing("tasks")
    names = {t["name"] for t in tools}
    assert names == {f"loom_{op.replace('.', '_')}" for op in TASK_OPS} | {
        "loom_search",
        "loom_activate",
        "loom_op",
    }
    # 13 tools, ~3,450 tokens (chars/4) with headroom
    assert len(json.dumps({"tools": tools})) // 4 <= 4_500
    by = {t["name"]: t for t in tools}
    for name in ("loom_task_pull", "loom_task_create", "loom_task_complete", "loom_task_updates"):
        assert "WHEN NOT" in by[name]["description"], name
    # the claim needs the fence; task.create keeps its required `title` argument
    assert "fence" in by["loom_task_complete"]["inputSchema"]["properties"]
    assert "title" in by["loom_task_create"]["inputSchema"]["required"]


def test_catalogue_facet_names_the_toolset_and_readonly_is_unchanged() -> None:
    # experimental task ops: on their own roster, not on the `all` listing
    assert op_toolsets("task.pull") == ["tasks"]
    assert op_toolsets("inbox.list") == ["tasks"]
    assert op_toolsets("task.accept") == []
    assert op_toolsets("memory.set") == ["all", "atomic", "coding", "core"]
    # readonly is a projection of `all`, not of a curated roster: the
    # experimental task ops are not listed there, as before
    assert not any(t["name"].startswith("loom_task_") for t in _listing("readonly"))
