"""loom.integrations.ag2: Loom's agent verbs as AG2 tools and a recall policy,
over a fake Loom agent and through real AG2 runs with AG2's scripted model."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
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
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
    observation_scope,
)
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


@dataclasses.dataclass
class _TransportState:
    session_protection: SessionProtection | None = None


class _FakeAgent:
    """A loom.Agent: ``call`` serves the tools, ``memory`` the policy."""

    def __init__(
        self, reply: dict[str, Any] | Exception, memory: _FakeMemory | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self._reply = reply
        self.memory = memory or _FakeMemory()
        self.transport = _TransportState()

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


def test_the_policy_stays_within_memory_search_s_top_k_bound() -> None:
    memory = _FakeMemory()
    policy = LoomRecallPolicy(_FakeAgent({}, memory), top_k=600)  # type: ignore[arg-type]
    asyncio.run(policy.apply([], _asked("seat"), None))  # type: ignore[arg-type]
    ((_, kw),) = memory.searches
    assert kw["top_k"] == 500


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


@asynccontextmanager
async def _protected_agent(
    family: SessionProtection | None,
    respond: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[Agent]:
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("NO_PROXY", "*")
        environment.setenv("no_proxy", "*")
        agent = Agent(
            org="acme",
            namespace="support",
            agent="bot",
            base_url="http://inert.invalid",
            api_key="test-token",
            session_protection=family,
            pacing=False,
            coalesce_sets=False,
            auto_run=False,
            mentor_brief=False,
            retry={"max_attempts": 1},
        )
        agent.transport._client._transport = httpx.MockTransport(respond)
        try:
            yield agent
        finally:
            await agent.transport.close()


def test_protected_agent_mock_transport_ignores_inherited_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("no_proxy", "localhost")

    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Transport selection must not send a request")

    async def go() -> None:
        async with _protected_agent(None, respond) as loom:
            selected = loom.transport._client._transport_for_url(
                httpx.URL("http://inert.invalid/v1/memory.search")
            )
            assert isinstance(selected, httpx.MockTransport)
        assert os.environ["NO_PROXY"] == "localhost"
        assert os.environ["no_proxy"] == "localhost"

    asyncio.run(go())


@pytest.mark.parametrize("constructor_context", ["bound", "ambient"])
def test_policy_constructor_contributors_survive_client_rebinding(
    tmp_path: Path, constructor_context: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    async def go() -> None:
        async with _protected_agent(
            original if constructor_context == "bound" else None, respond
        ) as loom:
            if constructor_context == "ambient":
                with original.automatic():
                    policy = LoomRecallPolicy(loom)
            else:
                policy = LoomRecallPolicy(loom)
            loom.transport.session_protection = replacement
            original.mark()
            config = TrackingConfig(TestConfig("Unexpected model call"))
            native = AG2Agent(
                "assistant",
                config=config,
                middleware=[Middleware(AssemblerMiddleware, policies=[policy])],
            )
            with pytest.raises(AutomaticCaptureSuppressed):
                await native.ask("Keep this turn private")
            assert posts == [] and config.calls == []
            assert not replacement.protected

    asyncio.run(go())


def test_policy_retains_previous_callback_contributors(tmp_path: Path) -> None:
    contributor = SessionProtection.create(base=tmp_path / "contributor")
    bound = SessionProtection.create(base=tmp_path / "bound")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    async def go() -> None:
        async with _protected_agent(bound, respond) as loom:
            policy = LoomRecallPolicy(loom)
            events = _asked("First turn")
            with observation_scope((contributor,)):
                prompts, returned = await policy.apply(["base"], events, None)  # type: ignore[arg-type]
            assert prompts == ["base"] and returned is events
            loom.transport.session_protection = replacement
            contributor.mark()
            with pytest.raises(AutomaticCaptureSuppressed):
                await policy.apply([], _asked("Later turn"), None)  # type: ignore[arg-type]
            assert posts == ["/v1/memory.search"]
            assert not bound.protected and not replacement.protected

    asyncio.run(go())


def test_policy_entry_binding_marked_during_authentication_stops_dispatch(tmp_path: Path) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    callback = SessionProtection.create(base=tmp_path / "callback")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    async def go() -> None:
        async with _protected_agent(original, respond) as loom:
            policy = LoomRecallPolicy(loom)
            loom.transport.session_protection = callback

            class RebindingAuth(httpx.Auth):
                async def async_auth_flow(self, request: httpx.Request):
                    await asyncio.sleep(0)
                    callback.mark()
                    loom.transport.session_protection = replacement
                    yield request

            loom.transport._client.auth = RebindingAuth()
            with pytest.raises(AutomaticCaptureSuppressed):
                await policy.apply([], _asked("Guard this recall"), None)  # type: ignore[arg-type]
            assert posts == []
            assert not original.protected and not replacement.protected

    asyncio.run(go())


def test_policy_marked_recall_is_durable_before_native_prompt_and_blocks_next_call(
    tmp_path: Path,
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "mirrored": True,
                "results": [
                    {
                        "memory_key": "seat",
                        "memory_type": "semantic",
                        "content_excerpt": "private recalled content",
                        "distance": 0.1,
                        "similarity": 0.9,
                    }
                ],
            },
        )

    class ObservedPolicy(LoomRecallPolicy):
        async def apply(self, prompts: Any, events: Any, context: Any) -> Any:
            result = await super().apply(prompts, events, context)
            assert SessionProtection.open(original.pointer()).protected
            return result

    async def go() -> None:
        async with _protected_agent(original, respond) as loom:
            policy = ObservedPolicy(loom)
            config = TrackingConfig(TestConfig("Recalled.", "Unexpected second model call."))
            native = AG2Agent(
                "assistant",
                config=config,
                middleware=[Middleware(AssemblerMiddleware, policies=[policy])],
            )
            await native.ask("Recall a preference")
            assert "private recalled content" in config.calls[0].prompt[-1]
            loom.transport.session_protection = replacement
            with pytest.raises(AutomaticCaptureSuppressed):
                await native.ask("Another automatic recall")
            assert posts == ["/v1/memory.search"] and len(config.calls) == 1
            assert not replacement.protected

    asyncio.run(go())


def test_policy_failed_durable_observation_withholds_native_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = SessionProtection.create(base=tmp_path / "broken")
    bound = SessionProtection.create(base=tmp_path / "bound")
    posts: list[str] = []
    mark = SessionProtection.mark

    def fail_one(family: SessionProtection) -> None:
        if family == broken:
            raise SessionProtectionUnavailable()
        mark(family)

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"mirrored": True, "results": []})

    async def go() -> None:
        async with _protected_agent(bound, respond) as loom:
            with observation_scope((broken,)):
                policy = LoomRecallPolicy(loom)
            monkeypatch.setattr(SessionProtection, "mark", fail_one)
            config = TrackingConfig(TestConfig("Unexpected model call"))
            native = AG2Agent(
                "assistant",
                config=config,
                middleware=[Middleware(AssemblerMiddleware, policies=[policy])],
            )
            with pytest.raises(SessionProtectionUnavailable):
                await native.ask("Withhold unprotected recall")
            assert config.calls == [] and posts == ["/v1/memory.search"]
            assert SessionProtection.open(bound.pointer()).protected

    asyncio.run(go())


def test_protected_family_still_allows_model_selected_tools(tmp_path: Path) -> None:
    family = SessionProtection.create(base=tmp_path / "family")
    family.mark()
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"mirrored": True, "results": []})

    async def go() -> None:
        async with _protected_agent(family, respond) as loom:
            config = TrackingConfig(TestConfig(_recall_call({"query": "seat"}), "No match."))
            native = AG2Agent(
                "assistant", config=config, tools=loom_tools(loom, only=["loom_recall"])
            )
            reply = await native.ask("Use the recall tool")
            assert await reply.content() == "No match."
            assert posts == ["/v1/memory.search"] and family.protected

    asyncio.run(go())
