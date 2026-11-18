"""LoomContextProvider: Agent Framework's ContextProvider over a fake Loom agent —
what a run's model sees before it, what is sent to Loom after it, abstention,
sessions and the refusals."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_framework import (
    Agent,
    AgentLoopMiddleware,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    ContextProvider,
    Message,
)
from loom import Agent as LoomAgent
from loom import SyncAgent
from loom.integrations.agent_framework import LoomContextProvider, _loom_session
from loom.memory import MemorySearchHit, MemorySearchResult
from loom.session_protection import AutomaticCaptureSuppressed, SessionProtection


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

    async def set_from_messages(self, messages: list[dict[str, Any]], **kw: Any) -> None:
        self.calls.append(("set_from_messages", (messages,), kw))


@dataclasses.dataclass
class _TransportState:
    session_protection: SessionProtection | None = None


class _Loom:
    def __init__(self, memory: _FakeMemory) -> None:
        self.memory = memory
        self.transport = _TransportState()


class _Client(BaseChatClient):  # type: ignore[misc]
    """Answers with `replies` in turn and keeps what each call was sent."""

    def __init__(self, *replies: Message) -> None:
        super().__init__()
        self.replies = list(replies) or [Message(role="assistant", contents=["Noted."])]
        self.sent: list[tuple[list[Message], dict[str, Any]]] = []

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool, options: Any, **kw: Any
    ) -> Any:
        self.sent.append((list(messages), dict(options or {})))
        reply = self.replies[min(len(self.sent), len(self.replies)) - 1]
        if stream:

            async def updates() -> Any:
                yield ChatResponseUpdate(role="assistant", contents=list(reply.contents))

            return self._build_response_stream(updates())

        async def respond() -> ChatResponse:
            return ChatResponse(messages=[reply])

        return respond()


def _hit(key: str, text: str, **kw: Any) -> MemorySearchHit:
    return MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt=text, similarity=0.9, **kw
    )


def _provider(fake: _FakeMemory, **kw: Any) -> LoomContextProvider:
    return LoomContextProvider(_Loom(fake), **{"user_id": "alice", **kw})  # type: ignore[arg-type]


def _run(provider: LoomContextProvider, client: _Client, *texts: str) -> Any:
    agent = Agent(client=client, instructions="Be brief.", context_providers=[provider])

    async def go() -> Any:
        session = agent.create_session()
        for text in texts:
            await agent.run(text, session=session)
        return session

    return asyncio.run(go())


def _instructions(sent: tuple[list[Message], dict[str, Any]]) -> str:
    return str(sent[1].get("instructions") or "")


def test_it_is_an_agent_framework_context_provider() -> None:
    assert issubclass(LoomContextProvider, ContextProvider)
    assert _provider(_FakeMemory()).source_id == "loom"


def test_the_memories_reach_the_model_before_the_run() -> None:
    fake = _FakeMemory([_hit("k1", "Alice prefers aisle seats"), _hit("k2", "Alice is vegetarian")])
    client = _Client()
    _run(_provider(fake, top_k=3), client, "Where should I sit?")
    (search,) = [c for c in fake.calls if c[0] == "search"]
    assert search[1] == ("Where should I sit?",)
    assert search[2] == {
        "top_k": 3,
        "user_id": "alice",
        "namespace_scope": "this",
        "bodies": "full",
        "explain": False,
    }
    text = _instructions(client.sent[0])
    assert "Be brief." in text
    assert "1. Alice prefers aisle seats" in text and "2. Alice is vegetarian" in text


def test_the_turn_is_sent_to_loom_after_the_run() -> None:
    fake = _FakeMemory()
    session = _run(_provider(fake), _Client(), "I prefer aisle seats", "I fly to Lisbon")
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert [[(m["role"], m["content"]) for m in c[1][0]] for c in extracts] == [
        [("user", "I prefer aisle seats"), ("assistant", "Noted.")],
        [("user", "I fly to Lisbon"), ("assistant", "Noted.")],
    ]
    loom_session = _loom_session("alice", session.session_id)
    assert [c[2] for c in extracts] == [
        {"user_id": "alice", "session_id": loom_session, "defer": True}
    ] * 2


def test_the_loom_session_is_per_user_and_unambiguous() -> None:
    assert _loom_session("alice", "s1") != _loom_session("bob", "s1")
    assert _loom_session("a/b", "c") != _loom_session("a", "b/c")
    assert ":" not in _loom_session("a:b", "c")  # Loom's chunk keys are `chunk:<session>:<n>`


def test_only_user_and_assistant_text_is_sent() -> None:
    fake = _FakeMemory()
    call = Message(
        role="assistant",
        contents=[Content.from_function_call(call_id="c1", name="lookup", arguments="{}")],
    )
    turn = [
        Message(role="system", contents=["Be brief."]),
        Message(role="user", contents=["hi"]),
        Message(role="tool", contents=[Content.from_function_result(call_id="c0", result="42")]),
        Message(role="user", contents=["  "]),
    ]
    agent = Agent(client=_Client(call), context_providers=[_provider(fake, defer=False)])
    asyncio.run(agent.run(turn, session=agent.create_session()))
    ((_, (messages,), kw),) = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert [(m["role"], m["content"]) for m in messages] == [("user", "hi")]
    assert kw["defer"] is False


def test_abstention_and_rows_below_the_floor_add_nothing() -> None:
    weak = dataclasses.replace(_hit("k2", "weak"), below_threshold=True)
    client = _Client()
    _run(_provider(_FakeMemory([weak])), client, "q")
    assert "Loom" not in _instructions(client.sent[0])
    client = _Client()
    _run(_provider(_FakeMemory([_hit("k1", "strong")], abstain=True)), client, "q")
    assert "Loom" not in _instructions(client.sent[0])
    client = _Client()
    _run(_provider(_FakeMemory([weak]), honour_abstain=False), client, "q")
    assert "1. weak" in _instructions(client.sent[0])


def test_derived_rows_are_cut_to_top_k_after_filtering() -> None:
    weak = dataclasses.replace(_hit("w", "weak"), below_threshold=True)
    hits = [weak] + [_hit(f"k{i}", f"fact {i}") for i in range(4)]
    client = _Client()
    _run(_provider(_FakeMemory(hits), top_k=2), client, "q")
    text = _instructions(client.sent[0])
    assert "1. fact 0" in text and "2. fact 1" in text
    assert "weak" not in text and "fact 2" not in text


def test_a_run_without_user_text_reads_nothing() -> None:
    fake = _FakeMemory()
    provider = _provider(fake)
    agent = Agent(client=_Client(), context_providers=[provider])
    message = Message(
        role="user",
        contents=[Content.from_uri("https://example.com/a.png", media_type="image/png")],
    )
    asyncio.run(agent.run(message, session=agent.create_session()))
    assert not [c for c in fake.calls if c[0] == "search"]


def test_a_long_turn_is_sent_in_batches_the_server_takes() -> None:
    fake = _FakeMemory()
    messages = [Message(role="user", contents=[f"fact {i}"]) for i in range(401)]
    agent = Agent(client=_Client(), context_providers=[_provider(fake)])
    asyncio.run(agent.run(messages, session=agent.create_session()))
    sizes = [len(c[1][0]) for c in fake.calls if c[0] == "set_from_messages"]
    assert sizes == [400, 2]


def test_without_a_user_the_provider_reads_and_writes_the_namespace() -> None:
    fake = _FakeMemory()
    session = _run(_provider(fake, user_id=None), _Client(), "q")
    search, extract = fake.calls
    assert search[2]["user_id"] is None and extract[2]["user_id"] is None
    assert extract[2]["session_id"] == _loom_session("-", session.session_id)


def test_it_refuses_a_sync_agent_a_top_k_out_of_bounds_and_a_user_naming_none() -> None:
    with pytest.raises(TypeError, match="awaits"):
        LoomContextProvider(object.__new__(SyncAgent))
    for top_k in (0, 501):
        with pytest.raises(ValueError, match="top_k"):
            _provider(_FakeMemory(), top_k=top_k)
    for user in ("", "-", " ", " alice", "alice "):
        with pytest.raises(ValueError, match="user_id"):
            _provider(_FakeMemory(), user_id=user)


def test_without_agent_framework_the_import_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "agent_framework", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.agent_framework")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[agent-framework\]"):
        importlib.import_module("loom.integrations.agent_framework")


def test_a_real_agent_posts_the_provider_calls() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    loom = LoomAgent(org="acme", namespace="travel", agent="bot", base_url="http://x", api_key="k")
    loom.transport.post = post  # type: ignore[method-assign]
    session = _run(LoomContextProvider(loom, user_id="alice", top_k=3), _Client(), "seat")
    assert [p for p, _ in posts] == ["/v1/memory.search", "/v1/memory.set_from_messages"]
    search, extract = (body for _, body in posts)
    assert (search["query"], search["top_k"], search["user_id"]) == ("seat", 3, "alice")
    assert (search["namespace_scope"], search["bodies"], search["explain"]) == (
        "this",
        "full",
        False,
    )
    assert (extract["user_id"], extract["session_id"]) == (
        "alice",
        _loom_session("alice", session.session_id),
    )
    assert [m["content"] for m in extract["messages"]] == ["seat", "Noted."]


@pytest.mark.filterwarnings("ignore:.*AgentLoopMiddleware is experimental")
def test_a_loop_sends_the_users_turn_and_the_replies_once() -> None:
    fake = _FakeMemory()
    client = _Client(
        Message(role="assistant", contents=["Step one."]),
        Message(role="assistant", contents=["Step two."]),
    )
    agent = Agent(
        client=client,
        context_providers=[_provider(fake)],
        middleware=[AgentLoopMiddleware(lambda iteration, **_: iteration < 2)],
    )
    asyncio.run(agent.run("Plan my trip", session=agent.create_session()))
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert [[(m["role"], m["content"]) for m in c[1][0]] for c in extracts] == [
        [("user", "Plan my trip"), ("assistant", "Step one."), ("assistant", "Step two.")]
    ]
    searches = [c[1][0] for c in fake.calls if c[0] == "search"]
    assert searches[0] == "Plan my trip" and len(searches) == 2


def test_a_whole_conversation_passed_each_run_sends_only_its_new_turn() -> None:
    fake = _FakeMemory()
    agent = Agent(client=_Client(), context_providers=[_provider(fake)])
    history = [
        Message(role="user", contents=["I prefer aisle seats"]),
        Message(role="assistant", contents=["Noted."]),
        Message(role="user", contents=["I fly to Lisbon"]),
        Message(role="user", contents=["in May"]),
    ]
    asyncio.run(agent.run(history))
    (search,) = [c for c in fake.calls if c[0] == "search"]
    assert search[1] == ("in May",)  # the latest user message, as the other adapters search
    ((_, (messages,), _),) = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert [m["content"] for m in messages] == ["I fly to Lisbon", "in May", "Noted."]


@pytest.mark.filterwarnings("ignore:.*AgentLoopMiddleware is experimental")
def test_a_streamed_loop_sends_its_last_reply() -> None:
    fake = _FakeMemory()
    client = _Client(
        Message(role="assistant", contents=["Step one."]),
        Message(role="assistant", contents=["Step two."]),
    )
    agent = Agent(
        client=client,
        context_providers=[_provider(fake)],
        middleware=[AgentLoopMiddleware(lambda iteration, **_: iteration < 2)],
    )

    async def go() -> None:
        stream = agent.run("Plan my trip", session=agent.create_session(), stream=True)
        async for _ in stream:
            pass
        await stream.get_final_response()

    asyncio.run(go())
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert [[m["content"] for m in c[1][0]] for c in extracts] == [["Plan my trip", "Step two."]]


def _protected_agent(
    family: SessionProtection | None,
    respond: Callable[[httpx.Request], httpx.Response],
) -> LoomAgent:
    agent = LoomAgent(
        org="acme",
        namespace="travel",
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
    return agent


@pytest.mark.parametrize("constructor_context", ["bound", "ambient"])
def test_constructor_contributors_block_native_run_after_client_rebinding(
    tmp_path: Path, constructor_context: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    async def go() -> None:
        loom = _protected_agent(original if constructor_context == "bound" else None, respond)
        try:
            if constructor_context == "ambient":
                with original.automatic():
                    provider = LoomContextProvider(loom)
            else:
                provider = LoomContextProvider(loom)
            loom.transport.session_protection = replacement
            original.mark()
            native = Agent(client=_Client(), context_providers=[provider])
            with pytest.raises(AutomaticCaptureSuppressed):
                await native.run("Keep this turn private", session=native.create_session())
            assert posts == []
            assert not replacement.protected
        finally:
            await loom.transport.close()

    asyncio.run(go())


def test_marked_recall_reaches_native_model_before_capture_is_suppressed(tmp_path: Path) -> None:
    family = SessionProtection.create(base=tmp_path / "family")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "mirrored": True,
                "results": [
                    {
                        "memory_key": "example",
                        "memory_type": "semantic",
                        "content_excerpt": "private recalled content",
                        "distance": 0.1,
                        "similarity": 0.9,
                    }
                ],
            },
        )

    async def go() -> None:
        loom = _protected_agent(family, respond)
        client = _Client()
        native = Agent(client=client, context_providers=[LoomContextProvider(loom)])
        try:
            with pytest.raises(AutomaticCaptureSuppressed):
                await native.run("Recall a preference", session=native.create_session())
            assert family.protected
            assert "private recalled content" in _instructions(client.sent[0])
            assert posts == ["/v1/memory.search"]
        finally:
            await loom.transport.close()

    asyncio.run(go())


def test_before_run_contributor_survives_until_native_after_run(tmp_path: Path) -> None:
    contributor = SessionProtection.create(base=tmp_path / "contributor")
    bound = SessionProtection.create(base=tmp_path / "bound")
    posts: list[str] = []

    class ContributingProvider(LoomContextProvider):
        async def before_run(self, **kwargs: Any) -> None:
            with contributor.automatic():
                await super().before_run(**kwargs)

    class MarkingClient(_Client):
        def _inner_get_response(self, **kwargs: Any) -> Any:
            contributor.mark()
            return super()._inner_get_response(**kwargs)

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    async def go() -> None:
        loom = _protected_agent(bound, respond)
        native = Agent(client=MarkingClient(), context_providers=[ContributingProvider(loom)])
        try:
            with pytest.raises(AutomaticCaptureSuppressed):
                await native.run("Retain the callback contributor", session=native.create_session())
            assert posts == ["/v1/memory.search"]
            assert contributor.protected and not bound.protected
        finally:
            await loom.transport.close()

    asyncio.run(go())


def test_callback_binding_marked_during_authentication_prevents_native_dispatch(
    tmp_path: Path,
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    callback = SessionProtection.create(base=tmp_path / "callback")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    async def go() -> None:
        loom = _protected_agent(original, respond)
        provider = LoomContextProvider(loom)
        loom.transport.session_protection = callback

        class RebindingAuth(httpx.Auth):
            async def async_auth_flow(self, request: httpx.Request):
                await asyncio.sleep(0)
                callback.mark()
                loom.transport.session_protection = replacement
                yield request

        loom.transport._client.auth = RebindingAuth()
        native = Agent(client=_Client(), context_providers=[provider])
        try:
            with pytest.raises(AutomaticCaptureSuppressed):
                await native.run("Guard the callback binding", session=native.create_session())
            assert posts == []
            assert not original.protected and not replacement.protected
        finally:
            await loom.transport.close()

    asyncio.run(go())


def test_marked_native_extraction_batch_stops_following_batches(tmp_path: Path) -> None:
    family = SessionProtection.create(base=tmp_path / "family")
    batches: list[list[dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("set_from_messages"):
            batches.append(json.loads(request.content)["messages"])
            return httpx.Response(200, json={"mirrored": True})
        return httpx.Response(200, json={"results": []})

    async def go() -> None:
        loom = _protected_agent(family, respond)
        native = Agent(client=_Client(), context_providers=[LoomContextProvider(loom)])
        turns = [Message(role="user", contents=[f"fact {index}"]) for index in range(401)]
        try:
            with pytest.raises(AutomaticCaptureSuppressed):
                await native.run(turns, session=native.create_session())
            assert [len(batch) for batch in batches] == [400]
            assert family.protected
        finally:
            await loom.transport.close()

    asyncio.run(go())
