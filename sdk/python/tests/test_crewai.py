"""LoomCrewMemory: a CrewAI Memory over a fake Loom agent — what each memory
call sends, what a crew run asks of it (with a fake LLM, no network), and the
refusals for what Loom does not do."""

from __future__ import annotations

import dataclasses
import importlib
import sys
from typing import Any

import pytest
from crewai import Agent as CrewAgent
from crewai import Crew, Task
from crewai.llms.base_llm import BaseLLM
from crewai.memory.unified_memory import Memory
from crewai.tools.memory_tools import RememberTool
from loom import Agent, SyncAgent
from loom.errors import BackendUnavailable
from loom.integrations.crewai import LoomCrewMemory
from loom.memory import MemorySearchHit, MemorySearchResult
from pydantic import Field, ValidationError


class _FakeMemory:
    """A SyncAgent's memory client: blocking calls."""

    def __init__(self, hits: list[MemorySearchHit] | None = None, abstain: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._hits = hits or []
        self._abstain = abstain

    def search(self, query: str, **kw: Any) -> MemorySearchResult:
        self.calls.append(("search", (query,), kw))
        result = MemorySearchResult(self._hits)
        result.abstain = self._abstain
        return result

    def set(self, key: str, value: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("set", (key, value), kw))
        return {}

    def set_from_messages(self, messages: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        self.calls.append(("set_from_messages", (messages,), kw))
        return {}


def _hit(key: str, text: str, sim: float = 0.9) -> MemorySearchHit:
    return MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt=text, similarity=sim
    )


def _sync_agent(fake: _FakeMemory) -> SyncAgent:
    sync = object.__new__(SyncAgent)
    sync.memory = fake  # type: ignore[assignment]
    return sync


def _memory(fake: _FakeMemory, **kw: Any) -> LoomCrewMemory:
    return LoomCrewMemory(agent=_sync_agent(fake), **kw)


def test_it_is_a_crewai_memory_with_no_local_store() -> None:
    memory = _memory(_FakeMemory())
    assert isinstance(memory, Memory)
    assert getattr(memory, "_storage", None) is None  # no local store, embedder or LLM


def test_recall_searches_loom_and_returns_matches() -> None:
    fake = _FakeMemory([_hit("k1", "Alice prefers aisle seats", 0.91)])
    matches = _memory(fake, user_id="alice").recall("seat", categories=["travel"], limit=3)
    assert fake.calls == [
        (
            "search",
            ("seat",),
            {
                "top_k": 3,
                "tags": ["travel"],
                "include_tags": False,
                "bodies": "full",
                "explain": False,
                "auto_touch": True,
                "user_id": "alice",
                "namespace_scope": "this",
            },
        )
    ]
    assert [(m.record.id, m.record.content, m.score) for m in matches] == [
        ("k1", "Alice prefers aisle seats", 0.91)
    ]


def test_recall_returns_nothing_when_loom_abstains_unless_told_not_to() -> None:
    weak = _FakeMemory([_hit("k", "weak", 0.2)], abstain=True)
    assert _memory(weak).recall("seat") == []
    assert [m.record.id for m in _memory(weak, honour_abstain=False).recall("seat")] == ["k"]


def test_remember_writes_one_content_keyed_memory() -> None:
    fake = _FakeMemory()
    memory = _memory(fake, tags=["crew"], user_id="alice")
    first = memory.remember("Alice prefers aisle seats", categories=["travel"])
    again = memory.remember("  Alice prefers aisle seats ")
    with pytest.raises(ValueError, match="nothing to remember"):
        memory.remember("   ")
    assert first is not None and again is not None and first.id == again.id
    assert first.id.startswith("crewai/")
    assert fake.calls[0] == (
        "set",
        (first.id, "Alice prefers aisle seats"),
        {"tags": ["crew", "travel"], "user_id": "alice"},
    )


def test_remember_many_sends_the_texts_for_extraction() -> None:
    fake = _FakeMemory()
    assert (
        _memory(fake, tags=["crew"]).remember_many(["Task output one", " "], categories=["t"]) == []
    )
    assert fake.calls == [
        (
            "set_from_messages",
            ([{"role": "assistant", "content": "Task output one"}],),
            {"extra_tags": ["crew", "t"], "user_id": None, "defer": True},
        )
    ]


def test_extraction_is_loom_s() -> None:
    memory = _memory(_FakeMemory())
    assert memory.extract_memories("raw task output") == ["raw task output"]
    assert memory.extract_memories("  ") == []


def test_reset_and_forget_refuse() -> None:
    memory = _memory(_FakeMemory())
    with pytest.raises(NotImplementedError, match="memory.delete"):
        memory.reset()
    with pytest.raises(NotImplementedError, match="memory.delete"):
        memory.forget(scope="/")


class _FakeLLM(BaseLLM):
    """Answers every call at once and keeps the prompts it saw."""

    prompts: list[str] = Field(default_factory=list)

    def call(self, messages: Any, *args: Any, **kwargs: Any) -> str:
        self.prompts.append(str(messages))
        return "Thought: I know.\nFinal Answer: Alice wants an aisle seat."

    def supports_function_calling(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 8192


def test_a_crew_run_recalls_into_the_prompt_and_saves_its_output() -> None:
    fake = _FakeMemory([_hit("k1", "Alice flies to Lisbon every month")])
    llm = _FakeLLM(model="fake")
    agent = CrewAgent(role="travel agent", goal="book seats", backstory="", llm=llm)
    task = Task(description="Which seat does Alice want?", expected_output="a seat", agent=agent)
    crew = Crew(agents=[agent], tasks=[task], memory=_memory(fake))
    assert crew.kickoff().raw == "Alice wants an aisle seat."
    ops = [op for op, _, _ in fake.calls]
    assert ops[0] == "search" and "set_from_messages" in ops
    assert any("Alice flies to Lisbon every month" in p for p in llm.prompts)
    (messages,), _ = next((a, kw) for op, a, kw in fake.calls if op == "set_from_messages")
    assert "Alice wants an aisle seat." in messages[0]["content"]


def test_a_real_sync_agent_posts_memory_search() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    with SyncAgent(
        org="acme", namespace="research", agent="bot", base_url="http://x", api_key="k"
    ) as sa:
        sa._agent.transport.post = post  # type: ignore[method-assign]
        assert LoomCrewMemory(agent=sa, user_id="alice").recall("seat", limit=2) == []
    ((path, body),) = posts
    assert path == "/v1/memory.search"
    assert (body["query"], body["top_k"], body["user_id"]) == ("seat", 2, "alice")
    assert body["namespace_scope"] == "this"


def test_without_crewai_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "crewai.memory.types", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.crewai")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[crewai\]"):
        importlib.import_module("loom.integrations.crewai")


def test_recall_leaves_out_rows_below_the_floor_then_caps_at_limit() -> None:
    weak = dataclasses.replace(_hit("k2", "weak", 0.2), below_threshold=True)
    fake = _FakeMemory([_hit("k1", "aisle seats"), weak, _hit("k3", "vegetarian")])
    assert [m.record.id for m in _memory(fake).recall("seat", limit=2)] == ["k1", "k3"]
    loose = _memory(fake, honour_abstain=False).recall("seat", limit=2)
    assert [m.record.id for m in loose] == ["k1", "k2"]


def test_an_async_loom_agent_is_refused() -> None:
    agent = Agent(org="acme", namespace="research", agent="bot", base_url="http://x", api_key="k")
    with pytest.raises(ValidationError, match="SyncAgent"):
        LoomCrewMemory(agent=agent)


def test_the_scope_tree_and_private_records_are_refused() -> None:
    memory = _memory(_FakeMemory())
    refused = (
        lambda: memory.scope("/agent/a"),
        lambda: memory.slice(["/agent/a"]),
        lambda: memory.update("crewai/k", content="x"),
        memory.list_scopes,
        memory.list_records,
        memory.info,
        memory.tree,
        memory.list_categories,
        memory.reset_all,
    )
    for call in refused:
        with pytest.raises(NotImplementedError):
            call()
    with pytest.raises(NotImplementedError, match="private"):
        memory.remember("Alice prefers aisle seats", private=True)
    with pytest.raises(NotImplementedError, match="private"):
        memory.remember_many(["Alice prefers aisle seats"], private=True)


def test_a_read_only_memory_saves_nothing() -> None:
    fake = _FakeMemory()
    memory = _memory(fake, read_only=True)
    assert memory.remember("Alice prefers aisle seats") is None
    assert memory.remember_many(["Alice prefers aisle seats"]) == []
    assert fake.calls == []
    memory.recall("seat")
    ((op, _, kw),) = fake.calls
    assert (op, kw["auto_touch"]) == ("search", False)  # a read records no usage


def test_the_remember_tool_saves_through_loom() -> None:
    fake = _FakeMemory()
    tool = RememberTool(memory=_memory(fake))
    assert tool._run(contents="Alice prefers aisle seats").startswith("Saved to memory")
    ((op, (key, value), _),) = fake.calls
    assert (op, value) == ("set", "Alice prefers aisle seats") and key.startswith("crewai/")
    with pytest.raises(ValueError, match="nothing to remember"):
        tool._run(contents="  ")


def test_recall_keeps_the_memory_s_tags_and_narrows_by_category() -> None:
    travel = dataclasses.replace(_hit("k1", "Alice prefers aisle seats"), tags=("crew-a", "travel"))
    food = dataclasses.replace(_hit("k2", "Alice is vegetarian"), tags=("crew-a", "food"))
    fake = _FakeMemory([travel, food])
    matches = _memory(fake, tags=["crew-a"]).recall("Alice", categories=["travel"])
    assert [m.record.id for m in matches] == ["k1"]
    ((_, _, kw),) = fake.calls
    assert (kw["tags"], kw["include_tags"], kw["top_k"]) == (["crew-a"], True, 50)
    assert (kw["bodies"], kw["explain"]) == ("full", False)  # whole bodies above top_k 15
    fake.calls.clear()
    _memory(fake).recall("Alice", categories=["travel"])
    ((_, _, kw),) = fake.calls
    assert (kw["tags"], kw["include_tags"]) == (["travel"], False)


def test_a_narrowed_recall_finds_a_category_past_the_first_limit_hits() -> None:
    others = [
        dataclasses.replace(_hit(f"k{i}", f"Alice note {i}", 0.9), tags=("crew-a", "travel"))
        for i in range(12)
    ]
    billing = dataclasses.replace(_hit("b", "Alice pays by card", 0.5), tags=("crew-a", "billing"))

    class _TopK(_FakeMemory):
        def search(self, query: str, **kw: Any) -> MemorySearchResult:
            return MemorySearchResult(super().search(query, **kw)[: kw["top_k"]])

    fake = _TopK([*others, billing])
    matches = _memory(fake, tags=["crew-a"]).recall("Alice", categories=["billing"], limit=10)
    assert [m.record.id for m in matches] == ["b"]


def test_a_narrowed_recall_stays_within_memory_search_s_top_k_bound() -> None:
    fake = _FakeMemory()
    _memory(fake, tags=["crew-a"]).recall("Alice", categories=["billing"], limit=200)
    ((_, _, kw),) = fake.calls
    assert kw["top_k"] == 500


def test_an_unnarrowed_recall_stays_within_memory_search_s_top_k_bound() -> None:
    fake = _FakeMemory()
    _memory(fake).recall("Alice", limit=600)
    ((_, _, kw),) = fake.calls
    assert kw["top_k"] == 500


def test_a_crew_runs_on_when_loom_is_unreachable() -> None:
    class _Down(_FakeMemory):
        def search(self, query: str, **kw: Any) -> MemorySearchResult:
            raise BackendUnavailable("loom unreachable")

        def set_from_messages(self, messages: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
            raise BackendUnavailable("loom unreachable")

    llm = _FakeLLM(model="fake")
    agent = CrewAgent(role="travel agent", goal="book seats", backstory="", llm=llm)
    task = Task(description="Which seat does Alice want?", expected_output="a seat", agent=agent)
    crew = Crew(agents=[agent], tasks=[task], memory=_memory(_Down()))
    assert crew.kickoff().raw == "Alice wants an aisle seat."  # no memory, no error
