"""LoomKnowledgeRetriever and LoomMemoryRetriever: LangChain's BaseRetriever
over a fake Loom agent — the search fields each sends, the Documents each
returns, abstention, and which agent serves invoke and ainvoke."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.retrievers import BaseRetriever
from langchain_core.tools.retriever import create_retriever_tool
from loom import Agent
from loom.integrations import _session_protection as callback_protection
from loom.integrations.langchain import LoomKnowledgeRetriever, LoomMemoryRetriever
from loom.knowledge import SearchHit
from loom.memory import MemorySearchHit, MemorySearchResult
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
    observation_scope,
)
from loom.sync import SyncAgent


class _FakeSearch:
    def __init__(self, result: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def search(self, query: str, **kw: Any) -> Any:
        self.calls.append({"query": query, **kw})
        return self._result


class _SyncSearch(_FakeSearch):
    def search(self, query: str, **kw: Any) -> Any:  # type: ignore[override]
        self.calls.append({"query": query, **kw})
        return self._result


def _agent(**clients: Any) -> SimpleNamespace:
    return SimpleNamespace(namespace="support", **clients)


def _sync_agent(**clients: Any) -> SyncAgent:
    """A SyncAgent whose clients are fakes; no loop or transport behind it."""
    agent = object.__new__(SyncAgent)
    agent._agent = SimpleNamespace(namespace="support")
    for name, client in clients.items():
        setattr(agent, name, client)
    return agent


def _chunk(uri: str, idx: int, text: str, sim: float = 0.8, namespace: str = "") -> SearchHit:
    return SearchHit(
        source_uri=uri,
        chunk_idx=idx,
        content=text,
        tags=["policy"],
        distance=1 - sim,
        similarity=sim,
        namespace=namespace,
        section="Refunds",
    )


def _memory(key: str, text: str, sim: float = 0.8, valid_at: str = "") -> MemorySearchHit:
    return MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=text,
        similarity=sim,
        valid_at=valid_at,
    )


def _memories(hits: list[MemorySearchHit], abstain: bool = False) -> MemorySearchResult:
    result = MemorySearchResult(hits)
    result.abstain = abstain
    return result


def test_both_are_langchain_retrievers() -> None:
    assert issubclass(LoomKnowledgeRetriever, BaseRetriever)
    assert issubclass(LoomMemoryRetriever, BaseRetriever)


def test_knowledge_documents_carry_the_chunk_and_where_it_came_from() -> None:
    knowledge = _FakeSearch([_chunk("doc://refunds", 2, "Annual plans refund pro rata.", 0.91234)])
    retriever = LoomKnowledgeRetriever(agent=_agent(knowledge=knowledge), k=3)
    docs = asyncio.run(retriever.ainvoke("refund policy"))
    # namespace_scope is always sent: the server's own default is all_mine
    assert knowledge.calls == [{"query": "refund policy", "top_k": 3, "namespace_scope": "this"}]
    assert len(docs) == 1
    assert docs[0].page_content == "Annual plans refund pro rata."
    assert docs[0].id == "support/doc://refunds#2"
    assert docs[0].metadata == {
        "source_uri": "doc://refunds",
        "chunk_idx": 2,
        "similarity": 0.9123,
        "section": "Refunds",
        "namespace": "support",
        "tags": ["policy"],
    }


def test_a_fanned_out_hit_is_named_by_its_own_namespace() -> None:
    knowledge = _FakeSearch([_chunk("doc://refunds", 0, "text", namespace="billing")])
    retriever = LoomKnowledgeRetriever(
        agent=_agent(knowledge=knowledge), namespace_scope="all_mine"
    )
    (doc,) = asyncio.run(retriever.ainvoke("refunds"))
    assert doc.id == "billing/doc://refunds#0" and doc.metadata["namespace"] == "billing"


def test_knowledge_search_fields_are_sent_only_when_set() -> None:
    knowledge = _FakeSearch([])
    retriever = LoomKnowledgeRetriever(
        agent=_agent(knowledge=knowledge),
        search_tags=["policy"],
        uri_prefix=["doc://handbook/"],
        namespace_scope="all_mine",
        search_kwargs={"search_mode": "lexical", "top_k": 99},
    )
    asyncio.run(retriever.ainvoke("refunds"))
    assert knowledge.calls == [
        {
            "query": "refunds",
            "search_mode": "lexical",
            "top_k": 4,  # k wins over a top_k in search_kwargs
            "tags": ["policy"],
            "uri_prefix": ["doc://handbook/"],
            "namespace_scope": "all_mine",
        }
    ]


def test_memory_documents_carry_the_key_and_score() -> None:
    memory = _FakeSearch(
        _memories(
            [
                _memory("prefs/seat", "Alice prefers aisle seats", 0.9, "2026-09-01"),
                _memory("prefs/meal", "Alice is vegetarian", 0.7),
            ]
        )
    )
    retriever = LoomMemoryRetriever(
        agent=_agent(memory=memory),
        k=1,
        search_tags=["customer"],
        memory_type="preference",
        user_id="alice",
        session_id="s-1",
        run_id="r-1",
        agent_id="planner",
    )
    docs = asyncio.run(retriever.ainvoke("seating"))
    assert memory.calls == [
        {
            "query": "seating",
            "top_k": 1,
            "bodies": "full",  # the stored clip whatever the k
            "explain": False,
            "tags": ["customer"],
            "memory_type": "preference",
            "user_id": "alice",
            "session_id": "s-1",
            "run_id": "r-1",
            "agent_id": "planner",
        }
    ]
    assert [d.page_content for d in docs] == ["Alice prefers aisle seats"]
    assert docs[0].id is None  # a key is unique per agent, user and session only
    assert docs[0].metadata == {
        "namespace": "support",
        "memory_key": "prefs/seat",
        "memory_type": "semantic",
        "similarity": 0.9,
        "source": "fact",
        "valid_at": "2026-09-01",
    }


def test_memory_abstain_returns_nothing_unless_disabled() -> None:
    weak = _memories([_memory("k", "a weak match", 0.2)], abstain=True)
    strict = LoomMemoryRetriever(agent=_agent(memory=_FakeSearch(weak)))
    assert asyncio.run(strict.ainvoke("anything")) == []
    loose = LoomMemoryRetriever(agent=_agent(memory=_FakeSearch(weak)), honour_abstain=False)
    assert [d.metadata["memory_key"] for d in asyncio.run(loose.ainvoke("anything"))] == ["k"]


def test_invoke_runs_on_a_sync_agent() -> None:
    knowledge = _SyncSearch([_chunk("doc://a", 0, "text")])
    retriever = LoomKnowledgeRetriever(agent=_sync_agent(knowledge=knowledge))
    assert [d.id for d in retriever.invoke("q")] == ["support/doc://a#0"]
    # ainvoke on a SyncAgent runs the blocking call off the event loop
    assert [d.id for d in asyncio.run(retriever.ainvoke("q"))] == ["support/doc://a#0"]
    assert len(knowledge.calls) == 2


def test_invoke_on_an_async_agent_says_to_use_ainvoke() -> None:
    retriever = LoomMemoryRetriever(agent=_agent(memory=_FakeSearch(_memories([]))))
    with pytest.raises(TypeError, match="ainvoke"):
        retriever.invoke("q")


def test_without_langchain_core_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "langchain_core.retrievers", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.langchain")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[langchain\]"):
        importlib.import_module("loom.integrations.langchain")


# The SDK's own clients, with only the HTTP boundary stubbed: a renamed client
# argument or a changed request body fails here.


class _Wire:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._reply = reply

    async def post(self, path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        self.posts.append((path, dict(payload)))
        return self._reply


_CHUNK = {
    "source_uri": "doc://refunds",
    "chunk_idx": 0,
    "content": "Annual plans refund pro rata.",
    "tags": [],
    "distance": 0.1,
    "similarity": 0.9,
}
_MEMORY = {
    "memory_key": "prefs/seat",
    "memory_type": "preference",
    "content_excerpt": "Alice prefers aisle seats",
    "distance": 0.1,
    "similarity": 0.9,
}


def _real_agent(wire: _Wire) -> Agent:
    agent = Agent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = wire.post  # type: ignore[method-assign]
    return agent


def test_the_knowledge_retriever_posts_knowledge_search_through_the_sdk() -> None:
    wire = _Wire({"results": [_CHUNK], "embedding_model": "m"})
    retriever = LoomKnowledgeRetriever(
        agent=_real_agent(wire), k=2, search_tags=["policy"], uri_prefix=["doc://"]
    )
    (doc,) = asyncio.run(retriever.ainvoke("refunds"))
    ((path, body),) = wire.posts
    assert path == "/v1/knowledge.search"
    assert body["org"] == "acme" and body["namespace"] == "support"
    assert {k: body[k] for k in ("query", "top_k", "tags", "uri_prefix", "namespace_scope")} == {
        "query": "refunds",
        "top_k": 2,
        "tags": ["policy"],
        "uri_prefix": ["doc://"],
        "namespace_scope": "this",
    }
    assert doc.id == "support/doc://refunds#0"


def test_the_memory_retriever_posts_memory_search_through_the_sdk() -> None:
    wire = _Wire({"results": [_MEMORY]})
    retriever = LoomMemoryRetriever(
        agent=_real_agent(wire),
        k=2,
        search_tags=["customer"],
        memory_type="preference",
        user_id="alice",
        session_id="s-1",
    )
    (doc,) = asyncio.run(retriever.ainvoke("seat"))
    ((path, body),) = wire.posts
    assert path == "/v1/memory.search"
    assert {
        k: body[k]
        for k in (
            "query",
            "top_k",
            "bodies",
            "explain",
            "tags",
            "memory_type",
            "user_id",
            "session_id",
        )
    } == {
        "query": "seat",
        "top_k": 2,
        "bodies": "full",
        "explain": False,
        "tags": ["customer"],
        "memory_type": "preference",
        "user_id": "alice",
        "session_id": "s-1",
    }
    assert doc.metadata["memory_key"] == "prefs/seat"


def test_invoke_on_a_real_sync_agent_posts_through_its_loop() -> None:
    wire = _Wire({"results": [_CHUNK], "embedding_model": "m"})
    with SyncAgent(
        org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k"
    ) as sa:
        sa._agent.transport.post = wire.post  # type: ignore[method-assign]
        (doc,) = LoomKnowledgeRetriever(agent=sa).invoke("refunds")
        assert [d.id for d in asyncio.run(LoomKnowledgeRetriever(agent=sa).ainvoke("refunds"))] == [
            doc.id
        ]
    assert [p for p, _ in wire.posts] == ["/v1/knowledge.search", "/v1/knowledge.search"]


def test_memory_below_the_floor_is_left_out_unless_disabled() -> None:
    weak = dataclasses.replace(_memory("k2", "a weak match", 0.2), below_threshold=True)
    # three rows for k=2: the floor is applied before the cut
    result = _memories([_memory("k1", "aisle seats", 0.9), weak, _memory("k3", "vegetarian", 0.8)])
    strict = LoomMemoryRetriever(agent=_agent(memory=_FakeSearch(result)), k=2)
    assert [d.metadata["memory_key"] for d in asyncio.run(strict.ainvoke("seat"))] == ["k1", "k3"]
    loose = LoomMemoryRetriever(agent=_agent(memory=_FakeSearch(result)), k=2, honour_abstain=False)
    assert [d.metadata["memory_key"] for d in asyncio.run(loose.ainvoke("seat"))] == ["k1", "k2"]


@contextmanager
def _native_agent(
    mode: str,
    family: SessionProtection | None,
    respond: Callable[[httpx.Request], httpx.Response],
) -> Iterator[Agent | SyncAgent]:
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("NO_PROXY", "*")
        environment.setenv("no_proxy", "*")
        agent_type = Agent if mode == "async" else SyncAgent
        agent = agent_type(
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
        mock = httpx.MockTransport(respond)
        agent.transport._client._transport = mock
        try:
            assert (
                agent.transport._client._transport_for_url(
                    httpx.URL("http://inert.invalid/v1/memory.search")
                )
                is mock
            )
            yield agent
        finally:
            if isinstance(agent, SyncAgent):
                agent.close()
            else:
                asyncio.run(agent.close())


def _native_retriever(agent: Agent | SyncAgent, kind: str) -> BaseRetriever:
    if kind == "knowledge":
        return LoomKnowledgeRetriever(
            agent=agent, k=2, search_tags=["policy"], uri_prefix=["doc://"]
        )
    assert kind == "memory"
    return LoomMemoryRetriever(
        agent=agent,
        k=2,
        search_tags=["customer"],
        memory_type="preference",
        user_id="alice",
        session_id="s-1",
        run_id="r-1",
    )


def _native_result(request: httpx.Request, *, mirrored: bool = False) -> httpx.Response:
    assert request.url.path in {"/v1/knowledge.search", "/v1/memory.search"}
    hit = _CHUNK if request.url.path == "/v1/knowledge.search" else _MEMORY
    return httpx.Response(200, json={"results": [hit], "mirrored": mirrored})


def _native_call(retriever: BaseRetriever, mode: str, query: str = "seat") -> Any:
    if mode == "invoke":
        return retriever.invoke(query)
    assert mode in {"async", "thread"}
    return asyncio.run(retriever.ainvoke(query))


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "invoke", "thread"])
@pytest.mark.parametrize("constructor", ["bound", "ambient", "both_bound", "both_ambient"])
def test_automatic_native_retrieval_retains_constructor_contributors(
    tmp_path: Path, kind: str, mode: str, constructor: str
) -> None:
    bound = SessionProtection.create(base=tmp_path / "bound")
    ambient = SessionProtection.create(base=tmp_path / "ambient")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _native_result(request)

    with _native_agent(mode, None if constructor == "ambient" else bound, respond) as agent:
        with observation_scope(() if constructor == "bound" else (ambient,)):
            retriever = _native_retriever(agent, kind)
        agent.transport.session_protection = current
        marked = ambient if constructor in {"ambient", "both_ambient"} else bound
        marked.mark()
        with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            _native_call(retriever, mode)
        assert posts == []
        assert not current.protected and not producer.protected
        assert (ambient if marked == bound else bound).protected is False


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "invoke", "thread"])
def test_native_retrieval_retains_an_earlier_callback_contributor(
    tmp_path: Path, kind: str, mode: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    earlier = SessionProtection.create(base=tmp_path / "earlier")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _native_result(request)

    with _native_agent(mode, original, respond) as agent:
        retriever = _native_retriever(agent, kind)
        with observation_scope((earlier,)):
            assert len(_native_call(retriever, mode)) == 1
        earlier.mark()
        agent.transport.session_protection = current
        with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            _native_call(retriever, mode, "later query")
        assert posts == [f"/v1/{kind}.search"]
        assert not original.protected and not current.protected and not producer.protected


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "invoke", "thread"])
def test_first_marked_native_recall_observes_every_family_before_return(
    tmp_path: Path, kind: str, mode: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    earlier = SessionProtection.create(base=tmp_path / "earlier")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    later = SessionProtection.create(base=tmp_path / "later")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _native_result(request, mirrored=len(posts) == 2)

    with _native_agent(mode, original, respond) as agent:
        retriever = _native_retriever(agent, kind)
        with observation_scope((earlier,)):
            _native_call(retriever, mode)
        agent.transport.session_protection = current
        with producer.automatic():
            (doc,) = _native_call(retriever, mode)
        assert doc.page_content == (
            _CHUNK["content"] if kind == "knowledge" else _MEMORY["content_excerpt"]
        )
        assert all(
            SessionProtection.open(root.pointer()).protected
            for root in (original, earlier, current, producer)
        )
        agent.transport.session_protection = later
        with later.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            _native_call(retriever, mode, "later automatic query")
        assert posts == [f"/v1/{kind}.search"] * 2
        assert not later.protected


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "thread"])
def test_native_retrieval_keeps_constructor_control_across_auth_await(
    tmp_path: Path, kind: str, mode: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    current = SessionProtection.create(base=tmp_path / "current")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _native_result(request)

    with _native_agent(mode, original, respond) as agent:
        retriever = _native_retriever(agent, kind)
        agent.transport.session_protection = current

        class RebindingAuth(httpx.Auth):
            async def async_auth_flow(self, request: httpx.Request) -> AsyncIterator[httpx.Request]:
                await asyncio.sleep(0)
                original.mark()
                agent.transport.session_protection = replacement
                yield request

        agent.transport._client.auth = RebindingAuth()
        with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            _native_call(retriever, mode)
        assert posts == []
        assert not current.protected and not replacement.protected and not producer.protected


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
def test_native_ainvoke_retains_entry_observers_before_to_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    entry = SessionProtection.create(base=tmp_path / "entry")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []
    to_thread = asyncio.to_thread

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _native_result(request)

    with _native_agent("thread", original, respond) as agent:
        retriever = _native_retriever(agent, kind)

        async def mark_before_worker(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
            entry.mark()
            agent.transport.session_protection = replacement
            return await to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", mark_before_worker)
        with (
            observation_scope((entry,)),
            producer.automatic(),
            pytest.raises(AutomaticCaptureSuppressed),
        ):
            _native_call(retriever, "thread")
        assert posts == []
        assert not original.protected and not replacement.protected and not producer.protected


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "invoke", "thread"])
def test_native_batch_stops_after_a_retained_family_is_marked(
    tmp_path: Path, kind: str, mode: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        original.mark()
        return _native_result(request)

    with _native_agent(mode, original, respond) as agent:
        retriever = _native_retriever(agent, kind)
        agent.transport.session_protection = current
        queries = ["first query", "second query", "third query"]
        with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            if mode == "invoke":
                retriever.batch(queries, config={"max_concurrency": 1})
            else:
                asyncio.run(retriever.abatch(queries, config={"max_concurrency": 1}))
        assert [body["query"] for body in posts] == ["first query"]
        assert not current.protected and not producer.protected
        assert queries == ["first query", "second query", "third query"]


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "invoke", "thread"])
def test_failed_retained_native_observation_withholds_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, mode: str
) -> None:
    broken = SessionProtection.create(base=tmp_path / "broken")
    current = SessionProtection.create(base=tmp_path / "current")
    mark = SessionProtection.mark
    posts: list[str] = []

    def fail_one(family: SessionProtection) -> None:
        if family == broken:
            raise SessionProtectionUnavailable()
        mark(family)

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _native_result(request, mirrored=True)

    with _native_agent(mode, broken, respond) as agent:
        retriever = _native_retriever(agent, kind)
        agent.transport.session_protection = current
        monkeypatch.setattr(SessionProtection, "mark", fail_one)
        with pytest.raises(SessionProtectionUnavailable):
            _native_call(retriever, mode)
        assert posts == [f"/v1/{kind}.search"] and current.protected


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "invoke", "thread"])
def test_protected_native_retriever_tool_stays_explicit(
    tmp_path: Path, kind: str, mode: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    current = SessionProtection.create(base=tmp_path / "current")
    posts: list[tuple[str, dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, json.loads(request.content)))
        return _native_result(request, mirrored=True)

    with _native_agent(mode, original, respond) as agent:
        retriever = _native_retriever(agent, kind)
        agent.transport.session_protection = current
        original.mark()
        tool = create_retriever_tool(retriever, "search_context", "Search relevant context.")
        result = (
            tool.invoke({"query": "seat"})
            if mode == "invoke"
            else asyncio.run(tool.ainvoke({"query": "seat"}))
        )
        assert result == (_CHUNK["content"] if kind == "knowledge" else _MEMORY["content_excerpt"])
        assert len(posts) == 1 and posts[0][0] == f"/v1/{kind}.search"
        assert posts[0][1]["query"] == "seat"
        assert original.protected and current.protected


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "invoke", "thread"])
def test_clean_native_automatic_retrieval_preserves_payloads_and_documents(
    tmp_path: Path, kind: str, mode: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[tuple[str, dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, json.loads(request.content)))
        return _native_result(request)

    with _native_agent(mode, original, respond) as agent:
        with observation_scope((producer,)):
            retriever = _native_retriever(agent, kind)
        with producer.automatic():
            (doc,) = _native_call(retriever, mode)
        ((path, body),) = posts
        assert path == f"/v1/{kind}.search"
        assert (body["org"], body["namespace"], body["query"], body["top_k"]) == (
            "acme",
            "support",
            "seat",
            2,
        )
        if kind == "knowledge":
            assert (body["tags"], body["uri_prefix"], body["namespace_scope"]) == (
                ["policy"],
                ["doc://"],
                "this",
            )
            assert doc.id == "support/doc://refunds#0" and doc.page_content == _CHUNK["content"]
        else:
            assert (body["tags"], body["bodies"], body["explain"]) == (["customer"], "full", False)
            assert (body["memory_type"], body["user_id"], body["session_id"], body["run_id"]) == (
                "preference",
                "alice",
                "s-1",
                "r-1",
            )
            assert doc.id is None and doc.page_content == _MEMORY["content_excerpt"]
            assert doc.metadata["memory_key"] == "prefs/seat"
        assert not original.protected and not producer.protected


def _run_native_callbacks(
    workers: list[threading.Thread],
    first_resolved: threading.Event,
    second_finished: threading.Event,
    release_first: threading.Event,
) -> None:
    workers[0].start()
    try:
        assert first_resolved.wait(10), "first callback did not reach root resolution"
        workers[1].start()
        assert second_finished.wait(10), "second callback did not finish"
    finally:
        release_first.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=10)
    assert not any(worker.is_alive() for worker in workers)


def test_concurrent_native_callbacks_retain_both_entry_contributors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound = SessionProtection.create(base=tmp_path / "bound")
    first = SessionProtection.create(base=tmp_path / "first")
    second = SessionProtection.create(base=tmp_path / "second")
    producer = SessionProtection.create(base=tmp_path / "producer")
    first_resolved = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    posts: list[dict[str, Any]] = []
    documents: dict[str, list[Any]] = {}
    errors: list[BaseException] = []
    resolve = callback_protection.response_roots

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return _native_result(request)

    def staged_roots(root: SessionProtection | None) -> tuple[SessionProtection, ...]:
        roots = resolve(root)
        if first in roots:
            first_resolved.set()
            assert release_first.wait(10), "first callback was not released"
        return roots

    with _native_agent("invoke", bound, respond) as agent:
        retriever = _native_retriever(agent, "knowledge")
        monkeypatch.setattr(callback_protection, "response_roots", staged_roots)

        def invoke(family: SessionProtection, query: str) -> None:
            try:
                with observation_scope((family,)):
                    documents[query] = retriever.invoke(query)
            except BaseException as error:  # noqa: BLE001 — worker errors must reach the test thread
                errors.append(error)
            finally:
                if family == second:
                    second_finished.set()

        workers = [
            threading.Thread(target=invoke, args=(first, "first"), daemon=True),
            threading.Thread(target=invoke, args=(second, "second"), daemon=True),
        ]
        _run_native_callbacks(workers, first_resolved, second_finished, release_first)
        assert errors == []
        assert sorted(documents) == ["first", "second"]
        assert all(
            [doc.page_content for doc in result] == [_CHUNK["content"]]
            for result in documents.values()
        )
        assert sorted(body["query"] for body in posts) == ["first", "second"]
        assert not any(root.protected for root in (bound, first, second, producer))
        second.mark()
        with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            retriever.invoke("later automatic query")
        assert len(posts) == 2
        assert not any(root.protected for root in (bound, first, producer))
