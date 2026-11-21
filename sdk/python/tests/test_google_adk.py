"""LoomMemoryService: ADK's memory service over a fake Loom agent — what each
method sends, abstention, and what an ADK Runner's model sees through
preload_memory and load_memory."""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.events import Event, EventActions
from google.adk.memory import BaseMemoryService
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.adk.tools import load_memory, preload_memory
from google.genai import types
from loom import Agent, SyncAgent
from loom.integrations.google_adk import LoomMemoryService, _entry_id, _loom_session
from loom.knowledge import SearchHit
from loom.memory import MemorySearchHit, MemorySearchResult
from pydantic import Field


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

    async def set_from_messages(self, messages: list[dict[str, object]], **kw: Any) -> dict:
        self.calls.append(("set_from_messages", (messages,), kw))
        return {}

    async def delete(self, key: str, **kw: Any) -> bool:
        self.calls.append(("delete", (key,), kw))
        return True


class _Knowledge:
    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search(self, query: str, **kw: Any) -> list[SearchHit]:
        self.calls.append((query, kw))
        return self.hits


class _Agent:
    def __init__(self, memory: _FakeMemory, knowledge: _Knowledge | None = None) -> None:
        self.memory = memory
        self.knowledge = knowledge or _Knowledge()

    async def call(self, op: str, **kw: Any) -> dict[str, Any]:
        self.memory.calls.append((op, (), kw))
        return {"tombstoned": True}


class _ScriptedLlm(BaseLlm):
    """Answers each model call with the next scripted content; keeps the requests."""

    model: str = "scripted"
    script: list[types.Content] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        yield LlmResponse(content=self.script.pop(0))


def _hit(key: str, text: str, **kw: Any) -> MemorySearchHit:
    kw.setdefault("version", 1)
    kw.setdefault("agent", "bot")
    return MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt=text, similarity=0.9, **kw
    )


def _user(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _model(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _service(fake: _FakeMemory, **kw: Any) -> LoomMemoryService:
    kw.setdefault("app_name", "travel")
    return LoomMemoryService(_Agent(fake), **kw)  # type: ignore[arg-type]


async def _run(
    service: LoomMemoryService, llm: _ScriptedLlm, *texts: str, **agent_kw: Any
) -> InMemorySessionService:
    sessions = InMemorySessionService()
    runner = Runner(
        app_name="travel",
        agent=LlmAgent(name="assistant", model=llm, **agent_kw),
        session_service=sessions,
        memory_service=service,
    )
    session = await sessions.create_session(app_name="travel", user_id="alice")
    for text in texts:
        message = types.Content(role="user", parts=[types.Part(text=text)])
        async for _ in runner.run_async(
            user_id="alice", session_id=session.id, new_message=message
        ):
            pass
    return sessions


def _prompt(request: LlmRequest) -> str:
    return " ".join(p.text or "" for c in request.contents for p in c.parts or [])


def test_it_is_an_adk_memory_service() -> None:
    assert issubclass(LoomMemoryService, BaseMemoryService)


def test_it_refuses_bad_construction() -> None:
    with pytest.raises(TypeError, match="ADK awaits"):
        LoomMemoryService(object.__new__(SyncAgent), app_name="travel")
    for top_k in (0, 501):
        with pytest.raises(ValueError, match="top_k"):
            _service(_FakeMemory(), top_k=top_k)
    with pytest.raises(ValueError, match="app_name"):
        _service(_FakeMemory(), app_name="")
    for prefix in ("", "/"):
        with pytest.raises(ValueError, match="key_prefix"):
            _service(_FakeMemory(), key_prefix=prefix)


def test_preload_memory_puts_the_matches_in_the_prompt() -> None:
    fake = _FakeMemory([_hit("adk/1", "Alice prefers aisle seats", valid_at="2026-11-01")])
    llm = _ScriptedLlm(script=[_model("An aisle seat.")])
    asyncio.run(_run(_service(fake), llm, "Which seat for alice?", tools=[preload_memory]))
    (search,) = [c for c in fake.calls if c[0] == "search"]
    assert search[1] == ("Which seat for alice?",)
    assert search[2] == {
        "top_k": 10,
        "user_id": "alice",
        "namespace_scope": "this",
        "bodies": "full",
        "explain": False,
    }
    prompt = _prompt(llm.requests[0])
    assert "Alice prefers aisle seats" in prompt and "Time: 2026-11-01" in prompt


def test_the_model_reads_load_memory_results() -> None:
    fake = _FakeMemory([_hit("adk/1", "Alice prefers aisle seats")])
    call = types.Content(
        role="model",
        parts=[
            types.Part(function_call=types.FunctionCall(name="load_memory", args={"query": "seat"}))
        ],
    )
    llm = _ScriptedLlm(script=[call, _model("An aisle seat.")])
    asyncio.run(_run(_service(fake), llm, "Which seat?", tools=[load_memory]))
    (response,) = [
        p.function_response
        for c in llm.requests[1].contents
        for p in c.parts or []
        if p.function_response
    ]
    (memory,) = response.response["result"].memories
    assert memory.id == _entry_id("adk/1", "-", "bot", 1)
    assert memory.content.parts[0].text == "Alice prefers aisle seats"


def test_search_leaves_out_rows_below_the_floor_and_an_abstention() -> None:
    hits = [_hit("adk/1", "kept"), _hit("adk/2", "weak", below_threshold=True)]
    found = asyncio.run(
        _service(_FakeMemory(hits)).search_memory(app_name="travel", user_id="u", query="q")
    )
    assert [m.id for m in found.memories] == [_entry_id("adk/1", "-", "bot", 1)]
    everything = _service(_FakeMemory(hits), honour_abstain=False)
    found = asyncio.run(everything.search_memory(app_name="travel", user_id="u", query="q"))
    assert [m.id for m in found.memories] == [
        _entry_id(k, "-", "bot", 1) for k in ("adk/1", "adk/2")
    ]
    abstained = _service(_FakeMemory(hits, abstain=True))
    assert not asyncio.run(
        abstained.search_memory(app_name="travel", user_id="u", query="q")
    ).memories


def test_search_cuts_to_top_k_and_skips_an_empty_query() -> None:
    fake = _FakeMemory([_hit(f"adk/{i}", f"fact {i}") for i in range(4)])
    found = asyncio.run(
        _service(fake, top_k=2).search_memory(app_name="travel", user_id="u", query="q")
    )
    assert [m.id for m in found.memories] == [
        _entry_id(k, "-", "bot", 1) for k in ("adk/0", "adk/1")
    ]
    assert not asyncio.run(
        _service(fake).search_memory(app_name="travel", user_id="u", query="  ")
    ).memories
    assert len(fake.calls) == 1


def test_a_saved_session_goes_to_extraction() -> None:
    fake = _FakeMemory()

    async def save(callback_context: Any) -> None:
        await callback_context.add_session_to_memory()

    llm = _ScriptedLlm(script=[_model("Noted.")])
    sessions = asyncio.run(
        _run(_service(fake), llm, "I prefer aisle seats", after_agent_callback=save)
    )
    (session,) = asyncio.run(sessions.list_sessions(app_name="travel", user_id="alice")).sessions
    (extract,) = [c for c in fake.calls if c[0] == "set_from_messages"]
    messages = extract[1][0]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "I prefer aisle seats"),
        ("assistant", "Noted."),
    ]
    assert all(str(m["timestamp"]).endswith("+00:00") for m in messages)
    # The session keys Loom's evidence of the turns.
    assert extract[2] == {
        "user_id": "alice",
        "session_id": _loom_session("alice", session.id),
        "defer": True,
    }


def test_the_documented_turn_callback_sends_each_turn_once() -> None:
    fake = _FakeMemory()

    async def save_turn(ctx: Any) -> None:
        turn = [e for e in ctx.session.events if e.invocation_id == ctx.invocation_id]
        await ctx.add_events_to_memory(events=turn)

    llm = _ScriptedLlm(script=[_model("Noted."), _model("Lisbon, then.")])
    sessions = asyncio.run(
        _run(
            _service(fake),
            llm,
            "I prefer aisle seats",
            "I fly to Lisbon",
            after_agent_callback=save_turn,
        )
    )
    (session,) = asyncio.run(sessions.list_sessions(app_name="travel", user_id="alice")).sessions
    calls = [c for c in fake.calls if c[0] == "set_from_messages"]
    sent = [[m["content"] for m in c[1][0]] for c in calls]
    assert sent == [["I prefer aisle seats", "Noted."], ["I fly to Lisbon", "Lisbon, then."]]
    # ADK's context passes the session; Loom's session is per user.
    assert [c[2]["session_id"] for c in calls] == [_loom_session("alice", session.id)] * 2


def test_extraction_leaves_out_thoughts_tool_calls_and_rewound_turns() -> None:
    def event(inv: str, author: str, *parts: types.Part, **kw: Any) -> Event:
        return Event(
            invocation_id=inv, author=author, content=types.Content(parts=list(parts)), **kw
        )

    session = Session(
        id="s1",
        app_name="travel",
        user_id="alice",
        events=[
            event("i1", "user", types.Part(text="I live in Lisbon")),
            event(
                "i1",
                "assistant",
                types.Part(text="planning", thought=True),
                types.Part(function_call=types.FunctionCall(name="f", args={})),
                types.Part(text="Lisbon it is"),
            ),
            event("i2", "user", types.Part(text="Actually Porto")),
            Event(
                invocation_id="i3",
                author="user",
                actions=EventActions(rewind_before_invocation_id="i2"),
            ),
        ],
    )
    fake = _FakeMemory()
    asyncio.run(_service(fake).add_session_to_memory(session))
    (extract,) = fake.calls
    assert [m["content"] for m in extract[1][0]] == ["I live in Lisbon", "Lisbon it is"]


def test_events_given_leave_out_a_turn_rewound_among_them() -> None:
    events = [
        Event(invocation_id="i1", author="user", content=_user("I live in Lisbon")),
        Event(invocation_id="i2", author="user", content=_user("Actually Porto")),
        Event(
            invocation_id="i3",
            author="user",
            actions=EventActions(rewind_before_invocation_id="i2"),
        ),
    ]
    fake = _FakeMemory()
    asyncio.run(
        _service(fake, defer=False).add_events_to_memory(
            app_name="travel", user_id="alice", events=events, session_id="s1"
        )
    )
    ((_, (messages,), kw),) = fake.calls
    assert [m["content"] for m in messages] == ["I live in Lisbon"]
    assert kw["session_id"] == _loom_session("alice", "s1")
    assert kw["defer"] is False  # extraction inline, as the service was built


def test_events_without_text_send_nothing() -> None:
    fake = _FakeMemory()
    silent = Event(author="user", content=types.Content(parts=[types.Part(text=" ")]))
    asyncio.run(
        _service(fake).add_events_to_memory(
            app_name="travel", user_id="u", events=[silent], session_id="s1"
        )
    )
    assert fake.calls == []


def test_events_without_a_session_are_refused() -> None:
    fake = _FakeMemory()
    event = Event(author="user", content=_user("I live in Lisbon"))
    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(
            _service(fake).add_events_to_memory(app_name="travel", user_id="u", events=[event])
        )
    assert fake.calls == []


def test_add_memory_writes_content_keyed_memories() -> None:
    fake = _FakeMemory()
    entries = [
        MemoryEntry(
            content=_user("Alice prefers aisle seats"), timestamp="2026-11-01T09:00:00+00:00"
        ),
        MemoryEntry(content=_user("  Alice prefers aisle seats ")),
        MemoryEntry(content=types.Content(parts=[types.Part(text="hidden", thought=True)])),
    ]
    asyncio.run(_service(fake).add_memory(app_name="travel", user_id="alice", memories=entries))
    (first, again) = fake.calls
    assert first[1][0] == again[1][0] and first[1][0].startswith("adk/")
    assert first[1][1] == "Alice prefers aisle seats"
    assert first[2]["user_id"] == "alice"
    # The user is in the key: Loom files a long value's passages by key alone.
    bob = _FakeMemory()
    asyncio.run(_service(bob).add_memory(app_name="travel", user_id="bob", memories=entries[:1]))
    assert bob.calls[0][1][0] != first[1][0]
    assert first[2]["valid_at"].isoformat() == "2026-11-01T09:00:00+00:00"
    assert again[2]["valid_at"] is None


def test_search_fills_remaining_slots_with_shared_app_knowledge() -> None:
    docs = _Knowledge([SearchHit("guide", 2, "Shared travel policy", [], 0.1, 0.9)])
    fake = _FakeMemory([_hit("seat", "Alice prefers aisle seats")])
    service = LoomMemoryService(_Agent(fake, docs), app_name="travel", top_k=2)  # type: ignore[arg-type]
    found = asyncio.run(service.search_memory(app_name="travel", user_id="alice", query="travel"))
    assert [entry.content.parts[0].text for entry in found.memories] == [
        "Alice prefers aisle seats",
        "Shared travel policy",
    ]
    assert found.memories[1].id is None
    assert found.memories[1].custom_metadata == {
        "source": "knowledge",
        "source_uri": "guide",
        "chunk_idx": 2,
    }
    assert docs.calls == [("travel", {"top_k": 1, "namespace_scope": "this"})]


def test_memory_abstention_does_not_hide_app_knowledge() -> None:
    docs = _Knowledge([SearchHit("guide", 0, "App policy", [], 0.1, 0.9)])
    service = LoomMemoryService(_Agent(_FakeMemory(abstain=True), docs), app_name="travel")  # type: ignore[arg-type]
    found = asyncio.run(service.search_memory(app_name="travel", user_id="alice", query="policy"))
    assert [entry.content.parts[0].text for entry in found.memories] == ["App policy"]


def test_a_timestamp_without_a_zone_is_local_time() -> None:
    fake = _FakeMemory()
    entry = MemoryEntry(content=_user("Alice moved to Porto"), timestamp="2026-11-01T09:00:00")
    asyncio.run(_service(fake).add_memory(app_name="travel", user_id="alice", memories=[entry]))
    ((_, _, kw),) = fake.calls
    assert kw["valid_at"] == datetime(2026, 11, 1, 9).astimezone()
    assert kw["valid_at"].tzinfo is not None


def test_a_timestamp_that_is_not_iso_is_no_valid_at() -> None:
    fake = _FakeMemory()
    entry = MemoryEntry(content=_user("Alice moved to Porto"), timestamp="last spring")
    asyncio.run(_service(fake).add_memory(app_name="travel", user_id="alice", memories=[entry]))
    ((_, _, kw),) = fake.calls
    assert kw["valid_at"] is None


def test_delete_memory_deletes_exact_version_and_skips_an_entry_without_one() -> None:
    fake = _FakeMemory()
    service = _service(fake)
    memory_id = _entry_id("user_seat", "adk-1", "writer", 1)
    asyncio.run(service.delete_memory(app_name="travel", user_id="alice", memory_id=memory_id))
    asyncio.run(service.delete_memory(app_name="travel", user_id="alice"))
    assert fake.calls == [
        (
            "memory.delete_version",
            (),
            {
                "memory_key": "user_seat",
                "version": "1",
                "agent_id": "writer",
                "user_id": "alice",
                "session_id": "adk-1",
            },
        )
    ]
    for bad in (
        "adk/1",
        '"ab"',
        '{"k":"s"}',
        '["k"]',
        '["k", "s"]',
        '["k","s","*","1"]',
        '["k","s","a",0]',
        '["k","s","a","0"]',
        '["k","s","a","18446744073709551616"]',
    ):
        with pytest.raises(ValueError, match="not an id"):
            asyncio.run(service.delete_memory(app_name="travel", user_id="alice", memory_id=bad))


def test_the_loom_session_is_per_user_and_unambiguous() -> None:
    assert _loom_session("alice", "main") != _loom_session("bob", "main")
    assert _loom_session("a/b", "c") != _loom_session("a", "b/c")
    assert _loom_session("alice", "main") == _loom_session("alice", "main")
    assert ":" not in _loom_session("a:b", "c")  # Loom's chunk keys are `chunk:<session>:<n>`


def test_a_call_for_another_app_is_refused() -> None:
    fake = _FakeMemory()
    service = _service(fake)
    calls = [
        service.search_memory(app_name="billing", user_id="alice", query="q"),
        service.add_memory(app_name="billing", user_id="alice", memories=[]),
        service.add_events_to_memory(app_name="billing", user_id="alice", events=[]),
        service.add_session_to_memory(Session(id="s", app_name="billing", user_id="alice")),
        service.delete_memory(app_name="billing", user_id="alice", memory_id="adk/1"),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="its own Loom namespace"):
            asyncio.run(call)
    assert fake.calls == []


def test_only_a_versioned_fact_gets_an_exact_deletion_id() -> None:
    fake = _FakeMemory(
        [
            _hit("user_seat", "a fact", session_id="adk-1"),
            _hit("adk/1", "an entry"),
            _hit("chunk:adk/1:0", "a passage", source="chunk", parent_memory_key="adk/1"),
            _hit("chunk:s1:0", "a turn", source="chunk", parent_memory_key="adk/1"),
            _hit("summary:s1", "a session", source="summary"),
        ]
    )
    service = _service(fake)
    found = asyncio.run(service.search_memory(app_name="travel", user_id="u", query="q"))
    ids = [m.id for m in found.memories]
    assert ids == [
        _entry_id("user_seat", "adk-1", "bot", 1),
        _entry_id("adk/1", "-", "bot", 1),
        None,
        None,
        None,
    ]
    assert [m.custom_metadata["source"] for m in found.memories] == [
        "fact",
        "fact",
        "chunk",
        "chunk",
        "summary",
    ]
    asyncio.run(service.delete_memory(app_name="travel", user_id="u", memory_id=ids[0]))
    assert fake.calls[-1] == (
        "memory.delete_version",
        (),
        {
            "memory_key": "user_seat",
            "version": "1",
            "agent_id": "bot",
            "user_id": "u",
            "session_id": "adk-1",
        },
    )


def test_without_google_adk_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google.adk.events", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.google_adk")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[google-adk\]"):
        importlib.import_module("loom.integrations.google_adk")


def test_a_real_agent_posts_the_service_calls() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    agent = Agent(org="acme", namespace="travel", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = post  # type: ignore[method-assign]
    service = LoomMemoryService(agent, app_name="travel", top_k=3)
    event = Event(author="user", content=_user("I prefer aisle seats"))
    entry = MemoryEntry(content=_user("Alice prefers aisle seats"))

    async def calls() -> None:
        await service.search_memory(app_name="travel", user_id="alice", query="seat")
        await service.add_events_to_memory(
            app_name="travel", user_id="alice", events=[event], session_id="s1"
        )
        await service.add_memory(app_name="travel", user_id="alice", memories=[entry])
        await service.delete_memory(
            app_name="travel", user_id="alice", memory_id=_entry_id("user_seat", "adk-1", "bot", 1)
        )

    asyncio.run(calls())
    paths = [p for p, _ in posts]
    assert paths == [
        "/v1/memory.search",
        "/v1/knowledge.search",
        "/v1/memory.set_from_messages",
        "/v1/memory.set",
        "/v1/memory.delete_version",
    ]
    search, knowledge, extract, write, delete = (body for _, body in posts)
    assert (search["query"], search["top_k"], search["user_id"]) == ("seat", 3, "alice")
    assert (search["namespace_scope"], search["bodies"], search["explain"]) == (
        "this",
        "full",
        False,
    )
    assert (extract["user_id"], extract["session_id"]) == ("alice", _loom_session("alice", "s1"))
    assert [m["content"] for m in extract["messages"]] == ["I prefer aisle seats"]
    assert write["memory_key"].startswith("adk/") and write["user_id"] == "alice"
    assert delete["agent"] == "bot" and delete["version"] == "1"
    assert (delete["memory_key"], delete["user_id"], delete["session_id"]) == (
        "user_seat",
        "alice",
        "adk-1",
    )


@pytest.mark.parametrize(
    "user",
    ["", "-", "*", " ", "\t", " - ", " alice", "alice ", "\u00a0alice"]
    + [user for char in "\x1c\x1d\x1e\x1f\x85" for user in (char, char + "alice", "alice" + char)],
)
def test_a_user_id_that_names_no_loom_user_is_refused(user: str) -> None:
    fake = _FakeMemory()
    service = _service(fake)
    entry = MemoryEntry(content=_user("Alice prefers aisle seats"))
    event = Event(author="user", content=_user("I prefer aisle seats"))

    calls = [
        service.add_session_to_memory(Session(id="s", app_name="travel", user_id=user)),
        service.search_memory(app_name="travel", user_id=user, query="seat"),
        service.add_memory(app_name="travel", user_id=user, memories=[entry]),
        service.add_events_to_memory(
            app_name="travel", user_id=user, events=[event], session_id="s1"
        ),
        service.delete_memory(app_name="travel", user_id=user, memory_id="adk/1"),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="user_id"):
            asyncio.run(call)
    assert fake.calls == []


def test_a_long_session_is_sent_in_batches_the_server_takes() -> None:
    fake = _FakeMemory()
    events = [Event(author="user", content=_user(f"fact {i}")) for i in range(401)]
    asyncio.run(
        _service(fake).add_events_to_memory(
            app_name="travel", user_id="alice", events=events, session_id="s1"
        )
    )
    sizes = [len(c[1][0]) for c in fake.calls if c[0] == "set_from_messages"]
    assert sizes == [400, 1]
    assert {c[2]["session_id"] for c in fake.calls} == {_loom_session("alice", "s1")}


def test_knowledge_search_obeys_its_wire_cap():
    agent = _Agent(_FakeMemory())
    service = LoomMemoryService(agent, app_name="travel", top_k=500)
    asyncio.run(service.search_memory(app_name="travel", user_id="alice", query="guide"))
    assert agent.knowledge.calls[0][1]["top_k"] == 100


@pytest.mark.parametrize("source", ["fact", "anchor"])
def test_same_version_different_writers_have_distinct_exact_deletion_ids(source: str) -> None:
    fake = _FakeMemory(
        [
            _hit(
                "seat",
                "Prefers aisle",
                agent="writer-a",
                session_id="shared",
                version=101,
                source=source,
            ),
            _hit(
                "seat",
                "Prefers window",
                agent="writer-b",
                session_id="shared",
                version=101,
                source=source,
            ),
        ]
    )
    service = _service(fake)
    found = asyncio.run(service.search_memory(app_name="travel", user_id="alice", query="seat"))
    first, second = found.memories
    assert first.id != second.id
    asyncio.run(service.delete_memory(app_name="travel", user_id="alice", memory_id=second.id))
    assert fake.calls[-1] == (
        "memory.delete_version",
        (),
        {
            "memory_key": "seat",
            "version": "101",
            "agent_id": "writer-b",
            "user_id": "alice",
            "session_id": "shared",
        },
    )


@pytest.mark.parametrize("source", ["fact", "anchor"])
def test_a_fact_without_writer_or_version_has_no_deletion_id(source: str) -> None:
    fake = _FakeMemory(
        [
            _hit("seat", "aisle", agent="", source=source),
            _hit("name", "Alice", version=0, source=source),
        ]
    )
    found = asyncio.run(_service(fake).search_memory(app_name="travel", user_id="alice", query="q"))
    assert [m.id for m in found.memories] == [None, None]
