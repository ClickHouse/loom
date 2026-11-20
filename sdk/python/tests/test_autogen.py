"""LoomMemory: AutoGen's Memory protocol over a fake Loom agent — what add,
query, update_context and clear send, abstention, and the context an
AssistantAgent's model sees."""

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
from autogen_core import Image
from autogen_core.memory import Memory, MemoryContent, MemoryMimeType
from autogen_core.model_context import BufferedChatCompletionContext
from autogen_core.models import AssistantMessage, SystemMessage, UserMessage
from loom import Agent, SyncAgent
from loom.integrations.autogen import LoomMemory
from loom.memory import MemorySearchHit, MemorySearchResult
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
    observation_scope,
)

# A 1x1 PNG.
_PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6"
    "kgAAAABJRU5ErkJggg=="
)


class _FakeMemory:
    def __init__(self, hits: list[MemorySearchHit] | None = None, abstain: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._hits = hits or []
        self._abstain = abstain

    async def search(self, query: str, **kw: Any) -> MemorySearchResult:
        self.calls.append(("search", (query,), kw))
        result = MemorySearchResult(self._hits)
        result.abstain = self._abstain
        return result

    async def set(self, key: str, value: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("set", (key, value), kw))
        return {}


@dataclasses.dataclass
class _TransportState:
    session_protection: SessionProtection | None = None


class _Agent:
    def __init__(self, memory: _FakeMemory) -> None:
        self.memory = memory
        self.transport = _TransportState()


def _hit(key: str, text: str, sim: float = 0.9) -> MemorySearchHit:
    return MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt=text, similarity=sim
    )


def _text(content: str) -> MemoryContent:
    return MemoryContent(content=content, mime_type=MemoryMimeType.TEXT)


def test_it_implements_autogens_memory() -> None:
    assert issubclass(LoomMemory, Memory)


def test_add_writes_one_content_keyed_memory() -> None:
    fake = _FakeMemory()
    memory = LoomMemory(_Agent(fake), user_id="alice", tags=["travel"])  # type: ignore[arg-type]
    asyncio.run(memory.add(_text("Alice prefers aisle seats")))
    asyncio.run(memory.add(_text("  Alice prefers aisle seats ")))
    asyncio.run(memory.add(_text("   ")))
    asyncio.run(memory.add(MemoryContent(content={"seat": "aisle"}, mime_type=MemoryMimeType.JSON)))
    (first, again, as_json) = [c for c in fake.calls if c[0] == "set"]
    assert first[1][0] == again[1][0] and first[1][0].startswith("autogen/")
    assert first[2] == {"tags": ["travel"], "user_id": "alice"}
    assert as_json[1][1] == '{"seat": "aisle"}'


def test_add_refuses_binary_content() -> None:
    memory = LoomMemory(_Agent(_FakeMemory()))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="text and JSON"):
        asyncio.run(memory.add(MemoryContent(content=b"\x00", mime_type=MemoryMimeType.BINARY)))


def test_query_maps_hits_and_honours_abstain() -> None:
    fake = _FakeMemory([_hit("k1", "Alice prefers aisle seats", 0.91)])
    memory = LoomMemory(_Agent(fake), user_id="alice", top_k=3)  # type: ignore[arg-type]
    result = asyncio.run(memory.query("seat"))
    assert fake.calls == [
        (
            "search",
            ("seat",),
            {
                "top_k": 3,
                "tags": None,
                "user_id": "alice",
                "namespace_scope": "this",
                "bodies": "full",
                "explain": False,
            },
        )
    ]
    assert [r.content for r in result.results] == ["Alice prefers aisle seats"]
    assert result.results[0].metadata == {
        "memory_key": "k1",
        "memory_type": "semantic",
        "similarity": 0.91,
        "source": "fact",
    }
    dated = dataclasses.replace(
        _hit("k2", "Alice moved to Lisbon"), source="chunk", valid_at="2026-01-01"
    )
    (row,) = asyncio.run(LoomMemory(_Agent(_FakeMemory([dated]))).query("move")).results  # type: ignore[arg-type]
    assert row.metadata == {
        "memory_key": "k2",
        "memory_type": "semantic",
        "similarity": 0.9,
        "source": "chunk",
        "valid_at": "2026-01-01",
    }
    weak = _FakeMemory([_hit("k", "weak", 0.2)], abstain=True)
    assert asyncio.run(LoomMemory(_Agent(weak)).query("seat")).results == []  # type: ignore[arg-type]
    loose = LoomMemory(_Agent(weak), honour_abstain=False)  # type: ignore[arg-type]
    assert len(asyncio.run(loose.query("seat")).results) == 1


def test_update_context_adds_memories_for_the_latest_user_message() -> None:
    fake = _FakeMemory([_hit("k1", "Alice prefers aisle seats")])
    memory = LoomMemory(_Agent(fake))  # type: ignore[arg-type]

    async def run() -> list[Any]:
        context = BufferedChatCompletionContext(buffer_size=10)
        await context.add_message(UserMessage(content="Book me a flight", source="user"))
        await context.add_message(AssistantMessage(content="Where to?", source="assistant"))
        await context.add_message(UserMessage(content="Lisbon, my usual seat", source="user"))
        result = await memory.update_context(context)
        assert [r.content for r in result.memories.results] == ["Alice prefers aisle seats"]
        return await context.get_messages()

    messages = asyncio.run(run())
    assert fake.calls[0][1] == ("Lisbon, my usual seat",)
    assert isinstance(messages[-1], SystemMessage)
    assert "Alice prefers aisle seats" in messages[-1].content


def test_update_context_adds_nothing_without_a_match() -> None:
    memory = LoomMemory(_Agent(_FakeMemory([], abstain=True)))  # type: ignore[arg-type]

    async def run() -> list[Any]:
        context = BufferedChatCompletionContext(buffer_size=10)
        await context.add_message(UserMessage(content="hi", source="user"))
        await memory.update_context(context)
        return await context.get_messages()

    assert len(asyncio.run(run())) == 1


def test_clear_refuses() -> None:
    fake = _FakeMemory()
    with pytest.raises(NotImplementedError, match="memory.delete"):
        asyncio.run(LoomMemory(_Agent(fake)).clear())  # type: ignore[arg-type]
    assert fake.calls == []


def test_a_top_k_below_one_is_refused_or_raised_to_one() -> None:
    with pytest.raises(ValueError, match="top_k"):
        LoomMemory(_Agent(_FakeMemory()), top_k=0)  # type: ignore[arg-type]
    fake = _FakeMemory()
    asyncio.run(LoomMemory(_Agent(fake)).query("seat", top_k=0))  # type: ignore[arg-type]
    ((_, _, kw),) = fake.calls
    assert kw["top_k"] == 1


def test_a_real_agent_posts_memory_search() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    agent = Agent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = post  # type: ignore[method-assign]
    assert asyncio.run(LoomMemory(agent, user_id="alice", top_k=2).query("seat")).results == []
    ((path, body),) = posts
    assert path == "/v1/memory.search"
    assert (body["query"], body["top_k"], body["user_id"]) == ("seat", 2, "alice")
    assert body["namespace_scope"] == "this"
    assert (body["bodies"], body["explain"]) == ("full", False)


def test_without_autogen_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "autogen_core.memory", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.autogen")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[autogen\]"):
        importlib.import_module("loom.integrations.autogen")


def test_query_leaves_out_rows_below_the_floor_then_caps_at_top_k() -> None:
    weak = dataclasses.replace(_hit("k2", "weak", 0.2), below_threshold=True)
    fake = _FakeMemory([_hit("k1", "aisle seats"), weak, _hit("k3", "vegetarian")])
    strict = asyncio.run(LoomMemory(_Agent(fake), top_k=2).query("seat"))  # type: ignore[arg-type]
    assert [r.metadata["memory_key"] for r in strict.results] == ["k1", "k3"]
    loose = asyncio.run(LoomMemory(_Agent(fake), top_k=2, honour_abstain=False).query("seat"))  # type: ignore[arg-type]
    assert [r.metadata["memory_key"] for r in loose.results] == ["k1", "k2"]


def test_a_sync_agent_is_refused() -> None:
    sync = object.__new__(SyncAgent)
    with pytest.raises(TypeError, match="loom.Agent"):
        LoomMemory(sync)  # type: ignore[arg-type]


def test_query_stays_within_memory_search_s_top_k_bound() -> None:
    fake = _FakeMemory()
    asyncio.run(LoomMemory(_Agent(fake)).query("seat", top_k=600))  # type: ignore[arg-type]
    ((_, _, kw),) = fake.calls
    assert kw["top_k"] == 500


def test_update_context_reads_a_multimodal_latest_message_by_its_text() -> None:
    fake = _FakeMemory()
    memory = LoomMemory(_Agent(fake))  # type: ignore[arg-type]

    async def run() -> None:
        context = BufferedChatCompletionContext(buffer_size=10)
        await context.add_message(UserMessage(content="Book me a flight", source="user"))
        picture = Image.from_base64(_PIXEL)
        await context.add_message(
            UserMessage(content=["Which seat should I pick?", picture], source="user")
        )
        await memory.update_context(context)

    asyncio.run(run())
    assert fake.calls[0][1] == ("Which seat should I pick?",)


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


def _reply(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/memory.search":
        return httpx.Response(200, json={"results": []})
    assert request.url.path == "/v1/memory.set"
    return httpx.Response(200, json={"stored": True})


def _mirrored_reply() -> httpx.Response:
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
        async with _protected_agent(None, respond) as agent:
            selected = agent.transport._client._transport_for_url(
                httpx.URL("http://inert.invalid/v1/memory.search")
            )
            assert isinstance(selected, httpx.MockTransport)
        assert os.environ["NO_PROXY"] == "localhost"
        assert os.environ["no_proxy"] == "localhost"

    asyncio.run(go())


@pytest.mark.parametrize("constructor_context", ["bound", "ambient"])
def test_context_update_retains_constructor_contributors_after_rebinding(
    tmp_path: Path, constructor_context: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _reply(request)

    async def go() -> None:
        async with _protected_agent(
            original if constructor_context == "bound" else None, respond
        ) as agent:
            if constructor_context == "ambient":
                with observation_scope((original,)):
                    memory = LoomMemory(agent)
            else:
                memory = LoomMemory(agent)
            agent.transport.session_protection = replacement
            original.mark()
            context = BufferedChatCompletionContext(buffer_size=10)
            await context.add_message(
                UserMessage(content="Keep this request private", source="user")
            )
            with pytest.raises(AutomaticCaptureSuppressed):
                await memory.update_context(context)
            assert posts == []
            assert not replacement.protected

    asyncio.run(go())


def test_context_update_retains_previous_callback_contributors(tmp_path: Path) -> None:
    contributor = SessionProtection.create(base=tmp_path / "contributor")
    bound = SessionProtection.create(base=tmp_path / "bound")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _reply(request)

    async def go() -> None:
        async with _protected_agent(bound, respond) as agent:
            memory = LoomMemory(agent)
            context = BufferedChatCompletionContext(buffer_size=10)
            await context.add_message(UserMessage(content="First request", source="user"))
            with observation_scope((contributor,)):
                result = await memory.update_context(context)
            assert result.memories.results == []
            contributor.mark()
            agent.transport.session_protection = replacement
            with pytest.raises(AutomaticCaptureSuppressed):
                await memory.update_context(context)
            assert posts == ["/v1/memory.search"]
            assert not bound.protected and not replacement.protected

    asyncio.run(go())


def test_context_update_freezes_entry_binding_before_native_message_await(tmp_path: Path) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    callback = SessionProtection.create(base=tmp_path / "callback")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _reply(request)

    async def go() -> None:
        async with _protected_agent(original, respond) as agent:
            memory = LoomMemory(agent)
            agent.transport.session_protection = callback

            class RebindingContext(BufferedChatCompletionContext):
                async def get_messages(self) -> list[Any]:
                    messages = await super().get_messages()
                    await asyncio.sleep(0)
                    callback.mark()
                    agent.transport.session_protection = replacement
                    return messages

            context = RebindingContext(buffer_size=10)
            await context.add_message(UserMessage(content="Guard the entry binding", source="user"))
            with pytest.raises(AutomaticCaptureSuppressed):
                await memory.update_context(context)
            assert posts == []
            assert not original.protected and not replacement.protected

    asyncio.run(go())


@pytest.mark.parametrize("following", ["context", "add"])
def test_marked_context_recall_is_durable_and_stops_later_automatic_work(
    tmp_path: Path, following: str
) -> None:
    family = SessionProtection.create(base=tmp_path / "family")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        if request.url.path == "/v1/memory.search":
            return _mirrored_reply()
        return _reply(request)

    class ObservedContext(BufferedChatCompletionContext):
        async def add_message(self, message: Any) -> None:
            if isinstance(message, SystemMessage):
                assert SessionProtection.open(family.pointer()).protected
            await super().add_message(message)

    async def go() -> None:
        async with _protected_agent(family, respond) as agent:
            memory = LoomMemory(agent)
            context = ObservedContext(buffer_size=10)
            await context.add_message(UserMessage(content="Recall a preference", source="user"))
            result = await memory.update_context(context)
            assert [entry.content for entry in result.memories.results] == [
                "private recalled content"
            ]
            messages = await context.get_messages()
            assert isinstance(messages[-1], SystemMessage)
            assert "private recalled content" in messages[-1].content
            agent.transport.session_protection = replacement
            with pytest.raises(AutomaticCaptureSuppressed):
                if following == "context":
                    await memory.update_context(context)
                else:
                    with producer.automatic():
                        await memory.add(_text("automatic capture"))
            assert posts == ["/v1/memory.search"]
            assert not replacement.protected and not producer.protected

    asyncio.run(go())


def test_context_recall_failed_retained_mark_withholds_native_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = SessionProtection.create(base=tmp_path / "broken")
    current = SessionProtection.create(base=tmp_path / "current")
    posts: list[str] = []
    mark = SessionProtection.mark

    def fail_one(family: SessionProtection) -> None:
        if family == broken:
            raise SessionProtectionUnavailable()
        mark(family)

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        assert request.url.path == "/v1/memory.search"
        return _mirrored_reply()

    async def go() -> None:
        async with _protected_agent(broken, respond) as agent:
            memory = LoomMemory(agent)
            agent.transport.session_protection = current
            monkeypatch.setattr(SessionProtection, "mark", fail_one)
            context = BufferedChatCompletionContext(buffer_size=10)
            await context.add_message(
                UserMessage(content="Withhold a failed observation", source="user")
            )
            with pytest.raises(SessionProtectionUnavailable):
                await memory.update_context(context)
            assert len(await context.get_messages()) == 1
            assert current.protected and posts == ["/v1/memory.search"]

    asyncio.run(go())


@pytest.mark.parametrize("operation", ["query", "add"])
def test_protected_family_allows_explicit_query_and_add_after_context_callback(
    tmp_path: Path, operation: str
) -> None:
    family = SessionProtection.create(base=tmp_path / "family")
    posts: list[tuple[str, dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, json.loads(request.content)))
        return _reply(request)

    async def go() -> None:
        async with _protected_agent(family, respond) as agent:
            memory = LoomMemory(agent)
            context = BufferedChatCompletionContext(buffer_size=10)
            await context.add_message(UserMessage(content="Ordinary request", source="user"))
            await memory.update_context(context)
            family.mark()
            posts.clear()
            if operation == "query":
                assert (await memory.query("explicit query")).results == []
                assert posts[0][1]["query"] == "explicit query"
            else:
                await memory.add(_text("explicit memory"))
                assert posts[0][1]["value"] == "explicit memory"
            expected = "/v1/memory.search" if operation == "query" else "/v1/memory.set"
            assert [path for path, _ in posts] == [expected]
            assert family.protected

    asyncio.run(go())
