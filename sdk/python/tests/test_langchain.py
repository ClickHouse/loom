"""LoomKnowledgeRetriever and LoomMemoryRetriever: LangChain's BaseRetriever
over a fake Loom agent — the search fields each sends, the Documents each
returns, abstention, and which agent serves invoke and ainvoke."""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.retrievers import BaseRetriever
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
    agent._agent = SimpleNamespace(namespace="support")  # type: ignore[attr-defined]
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
        agent=SimpleNamespace(memory=memory),
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
        "memory_key": "prefs/seat",
        "memory_type": "semantic",
        "similarity": 0.9,
        "source": "fact",
        "valid_at": "2026-09-01",
    }


def test_memory_abstain_returns_nothing_unless_disabled() -> None:
    weak = _memories([_memory("k", "a weak match", 0.2)], abstain=True)
    strict = LoomMemoryRetriever(agent=SimpleNamespace(memory=_FakeSearch(weak)))
    assert asyncio.run(strict.ainvoke("anything")) == []
    loose = LoomMemoryRetriever(
        agent=SimpleNamespace(memory=_FakeSearch(weak)), honour_abstain=False
    )
    assert [d.metadata["memory_key"] for d in asyncio.run(loose.ainvoke("anything"))] == ["k"]


def test_invoke_runs_on_a_sync_agent() -> None:
    knowledge = _SyncSearch([_chunk("doc://a", 0, "text")])
    retriever = LoomKnowledgeRetriever(agent=_sync_agent(knowledge=knowledge))
    assert [d.id for d in retriever.invoke("q")] == ["support/doc://a#0"]
    # ainvoke on a SyncAgent runs the blocking call off the event loop
    assert [d.id for d in asyncio.run(retriever.ainvoke("q"))] == ["support/doc://a#0"]
    assert len(knowledge.calls) == 2


def test_invoke_on_an_async_agent_says_to_use_ainvoke() -> None:
    retriever = LoomMemoryRetriever(agent=SimpleNamespace(memory=_FakeSearch(_memories([]))))
    with pytest.raises(TypeError, match="ainvoke"):
        retriever.invoke("q")


def test_without_langchain_core_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "langchain_core.retrievers", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.langchain")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[langchain\]"):
        importlib.import_module("loom.integrations.langchain")
