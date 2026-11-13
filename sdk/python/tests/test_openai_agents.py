"""loom_tools: Loom's agent verbs as OpenAI Agents SDK FunctionTools over a
fake Loom agent — names and schemas, the scope each call carries, and the
string the model reads back."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from typing import Any

import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext
from loom import Agent
from loom.errors import LoomError
from loom.integrations.openai_agents import loom_tools
from loom.tools import LOOM_TOOL_SPECS


class _FakeAgent:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self._reply = reply

    async def call(self, op: str, body: dict[str, Any], **scope: Any) -> dict[str, Any]:
        self.calls.append((op, dict(body), scope))
        return self._reply


def _invoke(tool: FunctionTool, arguments: dict[str, Any]) -> Any:
    raw = json.dumps(arguments)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=raw)
    return asyncio.run(tool.on_invoke_tool(ctx, raw))


def test_every_verb_is_a_function_tool_with_the_sdks_schema() -> None:
    tools = loom_tools(_FakeAgent({}))  # type: ignore[arg-type]
    assert all(isinstance(t, FunctionTool) for t in tools)
    assert [t.name for t in tools] == [s["name"] for s in LOOM_TOOL_SPECS]
    for tool, spec in zip(tools, LOOM_TOOL_SPECS, strict=True):
        assert tool.description == spec["description"]
        assert tool.params_json_schema == spec["parameters"]
        assert tool.strict_json_schema is False  # optional parameters stay optional


def test_only_keeps_a_subset() -> None:
    tools = loom_tools(_FakeAgent({}), only=["loom_recall", "loom_remember"])  # type: ignore[arg-type]
    assert [t.name for t in tools] == ["loom_remember", "loom_recall"]


def test_recall_runs_memory_search_in_the_bound_scope() -> None:
    agent = _FakeAgent(
        {
            "results": [
                {"memory_key": "seat", "content_excerpt": "Alice prefers aisle", "similarity": 0.91}
            ]
        }
    )
    (recall,) = loom_tools(agent, user_id="alice", session_id="s-1", only=["loom_recall"])  # type: ignore[arg-type]
    out = _invoke(recall, {"query": "seat preference", "top_k": 3})
    assert agent.calls == [
        (
            "memory.search",
            {"query": "seat preference", "top_k": 3},
            {"user_id": "alice", "session_id": "s-1", "run_id": None},
        )
    ]
    assert json.loads(out) == {
        "memories": [{"memory_key": "seat", "content": "Alice prefers aisle", "similarity": 0.91}]
    }


def test_remember_runs_memory_set() -> None:
    agent = _FakeAgent({"version": 2})
    (remember,) = loom_tools(agent, only=["loom_remember"])  # type: ignore[arg-type]
    out = _invoke(remember, {"memory_key": "seat", "value": "Alice prefers aisle seats"})
    assert agent.calls[0][:2] == (
        "memory.set",
        {"memory_key": "seat", "value": "Alice prefers aisle seats"},
    )
    assert json.loads(out)["stored"] is True


def test_without_openai_agents_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "agents", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.openai_agents")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[openai-agents\]"):
        importlib.import_module("loom.integrations.openai_agents")


def test_a_real_agent_posts_memory_search_in_the_bound_scope() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    agent = Agent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = post  # type: ignore[method-assign]
    (recall,) = loom_tools(agent, user_id="alice", only=["loom_recall"])
    assert json.loads(_invoke(recall, {"query": "seat", "top_k": 2})) == {"memories": []}
    ((path, body),) = posts
    assert path == "/v1/memory.search"
    assert (body["query"], body["top_k"], body["user_id"]) == ("seat", 2, "alice")


def test_a_refused_call_returns_the_error_to_the_model() -> None:
    (recall,) = loom_tools(_FakeAgent({}), only=["loom_recall"])  # type: ignore[arg-type]
    out = _invoke(recall, {"query": ""})
    assert out.startswith("loom_recall failed: ")
    assert "query" in out


def test_a_server_error_returns_the_error_to_the_model() -> None:
    class _Down(_FakeAgent):
        async def call(self, op: str, body: dict[str, Any], **scope: Any) -> dict[str, Any]:
            raise LoomError("memory store unavailable")

    (remember,) = loom_tools(_Down({}), only=["loom_remember"])  # type: ignore[arg-type]
    out = _invoke(remember, {"memory_key": "seat", "value": "aisle"})
    assert out == "loom_remember failed: memory store unavailable"


def test_max_result_chars_caps_what_the_model_reads() -> None:
    agent = _FakeAgent(
        {"results": [{"memory_key": "k", "content_excerpt": "x" * 500, "similarity": 0.9}]}
    )
    (recall,) = loom_tools(agent, only=["loom_recall"], max_result_chars=100)  # type: ignore[arg-type]
    out = _invoke(recall, {"query": "seat"})
    assert out.endswith("…[truncated]")
    assert len(out) == 100 + len("…[truncated]")
