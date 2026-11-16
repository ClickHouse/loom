"""LoomKnowledgeRetriever and LoomMemoryRetriever: LangChain's BaseRetriever
over a fake Loom agent — the search fields each sends, the Documents each
returns, abstention, and which agent serves invoke and ainvoke."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.retrievers import BaseRetriever
from loom import Agent
from loom.integrations.langchain import LoomKnowledgeRetriever, LoomMemoryRetriever
from loom.knowledge import SearchHit
from loom.memory import MemorySearchHit, MemorySearchResult
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
