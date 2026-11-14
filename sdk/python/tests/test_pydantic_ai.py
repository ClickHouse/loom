"""loom_toolset: Loom's agent verbs as a Pydantic AI FunctionToolset over a
fake Loom agent — names and schemas, the scope each call carries, and a run
with Pydantic AI's test model calling the tools."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from typing import Any

import pytest
from loom import Agent, SyncAgent
from loom.errors import LoomError
from loom.integrations.pydantic_ai import loom_toolset
from loom.tools import LOOM_TOOL_SPECS
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import FunctionToolset, ToolFailed
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.test import TestModel


class _FakeAgent:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self._reply = reply

    async def call(self, op: str, body: dict[str, Any], **scope: Any) -> dict[str, Any]:
        self.calls.append((op, dict(body), scope))
        return self._reply


def _tools(toolset: FunctionToolset[Any]) -> dict[str, Any]:
    return dict(toolset.tools)


def test_every_verb_is_a_tool_with_looms_schema() -> None:
    toolset = loom_toolset(_FakeAgent({}))  # type: ignore[arg-type]
    tools = _tools(toolset)
    assert list(tools) == [s["name"] for s in LOOM_TOOL_SPECS]
    for spec in LOOM_TOOL_SPECS:
        tool = tools[spec["name"]]
        assert tool.description == spec["description"]
        assert tool.function_schema.json_schema == spec["parameters"]


def test_only_keeps_a_subset() -> None:
    toolset = loom_toolset(_FakeAgent({}), only=["loom_recall"])  # type: ignore[arg-type]
    assert list(_tools(toolset)) == ["loom_recall"]


def test_a_run_calls_recall_in_the_bound_scope() -> None:
    hit = {"memory_key": "seat", "content_excerpt": "Alice prefers aisle", "similarity": 0.91}
    fake = _FakeAgent({"results": [hit]})
    toolset = loom_toolset(fake, user_id="alice", only=["loom_recall"])  # type: ignore[arg-type]
    model = TestModel(call_tools=["loom_recall"])
    result = PydanticAgent(model, toolsets=[toolset]).run_sync("Which seat does Alice like?")
    (op, body, scope) = fake.calls[0]
    assert op == "memory.search" and isinstance(body["query"], str)
    assert scope == {"user_id": "alice", "session_id": None, "run_id": None}
    assert "Alice prefers aisle" in result.output


def test_a_refused_call_returns_the_error_and_the_run_goes_on() -> None:
    class _Down(_FakeAgent):
        async def call(self, op: str, body: dict[str, Any], **scope: Any) -> dict[str, Any]:
            raise LoomError("memory store unavailable")

    toolset = loom_toolset(_Down({}), only=["loom_remember"])  # type: ignore[arg-type]
    model = TestModel(call_tools=["loom_remember"])
    result = PydanticAgent(model, toolsets=[toolset]).run_sync("Remember my seat")
    (returned,) = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert returned.outcome == "failed"  # a failed tool result, not a success
    assert returned.content == "loom_remember failed: memory store unavailable"


def test_a_rejected_argument_is_a_failed_tool_result() -> None:
    (recall,) = _tools(loom_toolset(_FakeAgent({}), only=["loom_recall"])).values()  # type: ignore[arg-type]
    with pytest.raises(ToolFailed, match='top_k must be an integer, got "five"'):
        asyncio.run(recall.function(query="seat", top_k="five"))


def test_max_result_chars_caps_what_the_model_reads() -> None:
    hit = {"memory_key": "k", "content_excerpt": "x" * 500, "similarity": 0.9}
    toolset = loom_toolset(
        _FakeAgent({"results": [hit]}),  # type: ignore[arg-type]
        only=["loom_recall"],
        max_result_chars=100,
    )
    out = asyncio.run(_tools(toolset)["loom_recall"].function(query="seat"))
    assert out.endswith("…[truncated]")
    assert len(out) == 100 + len("…[truncated]")


def test_a_real_agent_posts_memory_search() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    agent = Agent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = post  # type: ignore[method-assign]
    recall = _tools(loom_toolset(agent, user_id="alice", only=["loom_recall"]))["loom_recall"]
    out = asyncio.run(recall.function(query="seat", top_k=2))
    assert json.loads(out) == {"memories": []}
    ((path, body),) = posts
    assert path == "/v1/memory.search"
    assert (body["query"], body["top_k"], body["user_id"]) == ("seat", 2, "alice")


def test_without_pydantic_ai_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pydantic_ai", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.pydantic_ai")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[pydantic-ai\]"):
        importlib.import_module("loom.integrations.pydantic_ai")


def test_a_sync_agent_is_refused() -> None:
    with pytest.raises(TypeError, match="loom.Agent"):
        loom_toolset(object.__new__(SyncAgent))
