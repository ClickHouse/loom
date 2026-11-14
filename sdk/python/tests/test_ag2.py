"""loom.integrations.ag2: Loom's agent verbs as AG2 tools and a recall policy,
over a fake Loom agent and through real AG2 runs with AG2's scripted model."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import sys
from typing import Any

import pytest
from ag2 import Agent as AG2Agent
from ag2 import Middleware
from ag2.assembly import AssemblerMiddleware
from ag2.events import ModelRequest, TextInput, ToolCallEvent, ToolErrorEvent, ToolResultEvent
from ag2.testing import TestConfig, TrackingConfig
from ag2.tools.final import FunctionTool
from loom import Agent, SyncAgent
from loom.errors import LoomError
from loom.integrations.ag2 import LoomRecallPolicy, loom_tools
from loom.memory import MemorySearchHit, MemorySearchResult
from loom.tools import LOOM_TOOL_SPECS


class _FakeMemory:
    def __init__(self, hits: list[MemorySearchHit] | None = None, abstain: bool = False) -> None:
        self.searches: list[tuple[str, dict[str, Any]]] = []
        self._hits = hits or []
        self._abstain = abstain

    async def search(self, query: str, **kw: Any) -> MemorySearchResult:
        self.searches.append((query, kw))
        result = MemorySearchResult(self._hits)
        result.abstain = self._abstain
        return result


class _FakeAgent:
    """A loom.Agent: ``call`` serves the tools, ``memory`` the policy."""

    def __init__(
        self, reply: dict[str, Any] | Exception, memory: _FakeMemory | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self._reply = reply
        self.memory = memory or _FakeMemory()

    async def call(self, op: str, body: dict[str, Any], **scope: Any) -> dict[str, Any]:
        self.calls.append((op, dict(body), scope))
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _hit(key: str, text: str, sim: float = 0.9) -> MemorySearchHit:
    return MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt=text, similarity=sim
    )


def _recall_call(arguments: dict[str, Any]) -> ToolCallEvent:
    return ToolCallEvent(name="loom_recall", arguments=json.dumps(arguments))


def test_every_verb_is_a_function_tool_with_looms_schema() -> None:
    tools = loom_tools(_FakeAgent({}))  # type: ignore[arg-type]
    assert all(isinstance(t, FunctionTool) for t in tools)
    assert [t.name for t in tools] == [s["name"] for s in LOOM_TOOL_SPECS]
    for t, spec in zip(tools, LOOM_TOOL_SPECS, strict=True):
        assert t.schema.function.description == spec["description"]
        assert t.schema.function.parameters == spec["parameters"]


def test_only_keeps_a_subset() -> None:
    tools = loom_tools(_FakeAgent({}), only=["loom_recall", "loom_remember"])  # type: ignore[arg-type]
    assert [t.name for t in tools] == ["loom_remember", "loom_recall"]


def test_an_ag2_run_calls_recall_in_the_bound_scope_and_the_model_reads_it() -> None:
    hit = {"memory_key": "seat", "content_excerpt": "Alice prefers aisle", "similarity": 0.91}
    fake = _FakeAgent({"results": [hit]})
    config = TrackingConfig(TestConfig(_recall_call({"query": "seat", "top_k": 3}), "Aisle."))
    assistant = AG2Agent(
        "assistant",
        config=config,
        tools=loom_tools(fake, user_id="alice", session_id="s-1", only=["loom_recall"]),  # type: ignore[arg-type]
    )
    reply = asyncio.run(assistant.ask("Which seat does Alice like?"))
    assert asyncio.run(reply.content()) == "Aisle."
    ((op, body, scope),) = fake.calls
    assert (op, body["query"], body["top_k"]) == ("memory.search", "seat", 3)
    assert scope == {"user_id": "alice", "session_id": "s-1", "run_id": None}
    (result,) = [m for m in config.calls[-1].messages if isinstance(m, ToolResultEvent)]
    assert "Alice prefers aisle" in str(result.result)


def test_a_refused_call_reaches_the_model_as_a_tool_error_and_the_run_goes_on() -> None:
    fake = _FakeAgent(LoomError("memory store unavailable"))
    config = TrackingConfig(
        TestConfig(_recall_call({"query": "seat"}), "Sorry.", raise_tool_errors=False)
    )
    assistant = AG2Agent("assistant", config=config, tools=loom_tools(fake, only=["loom_recall"]))  # type: ignore[arg-type]
    reply = asyncio.run(assistant.ask("Which seat?"))
    assert asyncio.run(reply.content()) == "Sorry."
    (call,) = [m for m in config.calls[-1].messages if isinstance(m, ToolCallEvent)]
    (error,) = [m for m in config.calls[-1].messages if isinstance(m, ToolErrorEvent)]
    assert error.parent_id == call.id  # still the result of that call
    # What the model reads: Loom's message, not a traceback.
    assert [part.content for part in error.result.parts] == [
        "loom_recall failed: memory store unavailable"
    ]


def test_the_policy_adds_loom_s_memories_to_the_prompt_the_model_sees() -> None:
    memory = _FakeMemory([_hit("seat", "Alice prefers aisle seats")])
    policy = LoomRecallPolicy(_FakeAgent({}, memory), user_id="alice", tags=["travel"], top_k=3)  # type: ignore[arg-type]
    config = TrackingConfig(TestConfig("Aisle."))
    assistant = AG2Agent(
        "assistant",
        config=config,
        middleware=[Middleware(AssemblerMiddleware, policies=[policy])],
    )
    asyncio.run(assistant.ask("Which seat does Alice like?"))
    assert memory.searches == [
        (
            "Which seat does Alice like?",
            {
                "top_k": 3,
                "tags": ["travel"],
                "user_id": "alice",
                "namespace_scope": "this",
                "bodies": "full",
                "explain": False,
            },
        )
    ]
    assert (
        config.calls[0].prompt[-1]
        == "## Relevant memories from Loom\n\n- Alice prefers aisle seats"
    )


def test_the_policy_adds_nothing_when_loom_abstains_unless_told_not_to() -> None:
    async def apply(policy: LoomRecallPolicy) -> list[str]:
        prompts, _ = await policy.apply(["base"], _asked("seat?"), None)  # type: ignore[arg-type]
        return prompts

    weak = _FakeMemory([_hit("k", "weak", 0.2)], abstain=True)
    assert asyncio.run(apply(LoomRecallPolicy(_FakeAgent({}, weak)))) == ["base"]  # type: ignore[arg-type]
    loose = LoomRecallPolicy(_FakeAgent({}, weak), honour_abstain=False)  # type: ignore[arg-type]
    assert asyncio.run(apply(loose)) == ["base", "## Relevant memories from Loom\n\n- weak"]


def test_the_policy_leaves_out_rows_below_the_floor_then_caps_at_top_k() -> None:
    below = dataclasses.replace(_hit("k2", "weak", 0.2), below_threshold=True)
    memory = _FakeMemory(
        [_hit("k1", "aisle seats"), below, _hit("k3", "vegetarian"), _hit("k4", "window")]
    )
    policy = LoomRecallPolicy(_FakeAgent({}, memory), top_k=2)  # type: ignore[arg-type]
    prompts, _ = asyncio.run(policy.apply([], _asked("Alice"), None))  # type: ignore[arg-type]
    assert prompts == ["## Relevant memories from Loom\n\n- aisle seats\n- vegetarian"]


def test_the_policy_reads_the_user_s_request_and_skips_an_inbox_message() -> None:
    memory = _FakeMemory([_hit("k1", "aisle seats")])
    policy = LoomRecallPolicy(_FakeAgent({}, memory))  # type: ignore[arg-type]
    inbox = ModelRequest([TextInput("deploy finished")], origin="service")
    asyncio.run(policy.apply([], [*_asked("Which seat?"), inbox], None))  # type: ignore[arg-type]
    assert [q for q, _ in memory.searches] == ["Which seat?"]
    assert asyncio.run(policy.apply(["base"], [inbox], None)) == (["base"], [inbox])  # type: ignore[arg-type]
    assert len(memory.searches) == 1


def test_the_policy_searches_nothing_without_a_user_message() -> None:
    memory = _FakeMemory([_hit("k1", "aisle seats")])
    policy = LoomRecallPolicy(_FakeAgent({}, memory))  # type: ignore[arg-type]
    assert asyncio.run(policy.apply(["base"], [], None)) == (["base"], [])  # type: ignore[arg-type]
    assert memory.searches == []


def test_a_sync_agent_and_a_top_k_below_one_are_refused() -> None:
    sync = object.__new__(SyncAgent)
    with pytest.raises(TypeError, match="loom.Agent"):
        LoomRecallPolicy(sync)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="loom.Agent"):
        loom_tools(sync)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="top_k"):
        LoomRecallPolicy(_FakeAgent({}), top_k=0)  # type: ignore[arg-type]


def test_a_real_agent_posts_the_policy_s_memory_search() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    agent = Agent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = post  # type: ignore[method-assign]
    policy = LoomRecallPolicy(agent, user_id="alice", top_k=2)
    asyncio.run(policy.apply([], _asked("seat"), None))  # type: ignore[arg-type]
    ((path, body),) = posts
    assert path == "/v1/memory.search"
    keys = ("query", "top_k", "user_id", "namespace_scope", "bodies", "explain")
    assert {k: body[k] for k in keys} == {
        "query": "seat",
        "top_k": 2,
        "user_id": "alice",
        "namespace_scope": "this",
        "bodies": "full",
        "explain": False,
    }


def test_without_ag2_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ag2.events", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.ag2")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[ag2\]"):
        importlib.import_module("loom.integrations.ag2")


def _asked(text: str) -> list[Any]:
    return [ModelRequest([TextInput(text)])]
