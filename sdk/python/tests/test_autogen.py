"""LoomMemory: AutoGen's Memory protocol over a fake Loom agent — what add,
query, update_context and clear send, abstention, and the context an
AssistantAgent's model sees."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import sys
from typing import Any

import pytest
from autogen_core import Image
from autogen_core.memory import Memory, MemoryContent, MemoryMimeType
from autogen_core.model_context import BufferedChatCompletionContext
from autogen_core.models import AssistantMessage, SystemMessage, UserMessage
from loom import Agent, SyncAgent
from loom.integrations.autogen import LoomMemory
from loom.memory import MemorySearchHit, MemorySearchResult

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


class _Agent:
    def __init__(self, memory: _FakeMemory) -> None:
        self.memory = memory


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
    assert result.results[0].metadata["memory_key"] == "k1"
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
