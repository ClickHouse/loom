"""LoomKnowledgeRetriever and LoomMemoryRetriever: DSPy retrievers over a fake Loom
agent — the search fields each sends, the passages and sources each returns,
abstention, which agent serves a call and acall, and the ReAct tool."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import httpx
import pytest
from dspy.utils.dummies import DummyLM
from loom import Agent
from loom.integrations.dspy import LoomKnowledgeRetriever, LoomMemoryRetriever
from loom.knowledge import SearchHit
from loom.memory import MemorySearchHit, MemorySearchResult
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
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


class _Failing:
    def search(self, query: str, **kw: Any) -> Any:
        raise RuntimeError("loom is down")


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


def test_knowledge_passages_carry_the_chunk_and_where_it_came_from() -> None:
    knowledge = _SyncSearch([_chunk("doc://refunds", 2, "Annual plans refund pro rata.", 0.91234)])
    found = LoomKnowledgeRetriever(_sync_agent(knowledge=knowledge), k=3)("refund policy")
    # namespace_scope is always sent: the server's own default is all_mine
    assert knowledge.calls == [{"query": "refund policy", "top_k": 3, "namespace_scope": "this"}]
    assert isinstance(found, dspy.Prediction)
    assert found.passages == ["Annual plans refund pro rata."]
    assert found.sources == ["support/doc://refunds#2"]
    assert found.scores == [0.9123]
    assert found.metadata == [
        {
            "source_uri": "doc://refunds",
            "chunk_idx": 2,
            "similarity": 0.9123,
            "section": "Refunds",
            "namespace": "support",
            "tags": ["policy"],
        }
    ]


def test_a_fanned_out_hit_is_named_by_its_own_namespace() -> None:
    knowledge = _SyncSearch([_chunk("doc://refunds", 0, "text", namespace="billing")])
    found = LoomKnowledgeRetriever(_sync_agent(knowledge=knowledge), namespace_scope="all_mine")(
        "q"
    )
    assert knowledge.calls[0]["namespace_scope"] == "all_mine"
    assert found.sources == ["billing/doc://refunds#0"]


def test_knowledge_search_fields_are_sent_only_when_set() -> None:
    knowledge = _SyncSearch([])
    LoomKnowledgeRetriever(
        _sync_agent(knowledge=knowledge),
        k=5,
        search_tags=["policy"],
        uri_prefix=["doc://"],
        search_kwargs={"search_mode": "lexical", "top_k": 99},
    )("q")
    # k wins over a top_k passed through search_kwargs
    assert knowledge.calls == [
        {
            "query": "q",
            "search_mode": "lexical",
            "top_k": 5,
            "tags": ["policy"],
            "uri_prefix": ["doc://"],
            "namespace_scope": "this",
        }
    ]


def test_k_per_call_overrides_the_retrievers_own_and_cuts_extra_hits() -> None:
    hits = [_chunk("doc://a", i, f"chunk {i}") for i in range(3)]
    knowledge = _SyncSearch(hits)
    search = LoomKnowledgeRetriever(_sync_agent(knowledge=knowledge), k=3)
    assert search("q", k=2).passages == ["chunk 0", "chunk 1"]
    assert knowledge.calls[0]["top_k"] == 2


@pytest.mark.parametrize("k", [0, -1, 1.5, True, "3"])
def test_k_must_be_a_positive_int(k: Any) -> None:
    with pytest.raises(ValueError, match="k must be a positive int"):
        LoomKnowledgeRetriever(_sync_agent(knowledge=_SyncSearch([])), k=k)
    search = LoomKnowledgeRetriever(_sync_agent(knowledge=_SyncSearch([])))
    with pytest.raises(ValueError, match="k must be a positive int"):
        search("q", k=k)


def test_an_empty_result_is_an_empty_prediction() -> None:
    found = LoomKnowledgeRetriever(_sync_agent(knowledge=_SyncSearch([])))("q")
    assert (found.passages, found.sources, found.scores, found.metadata) == ([], [], [], [])


def test_a_search_error_is_raised_not_swallowed() -> None:
    search = LoomKnowledgeRetriever(_sync_agent(knowledge=_Failing()))
    with pytest.raises(RuntimeError, match="loom is down"):
        search("q")


def test_memory_passages_carry_the_key_and_score() -> None:
    memory = _SyncSearch(_memories([_memory("prefs/seat", "Alice prefers aisle seats", 0.87654)]))
    found = LoomMemoryRetriever(
        _sync_agent(memory=memory),
        k=2,
        search_tags=["customer"],
        memory_type="preference",
        user_id="alice",
        session_id="s-1",
        run_id="r-1",
        agent_id="bot",
    )("seat")
    assert memory.calls == [
        {
            "query": "seat",
            "top_k": 2,
            "bodies": "full",
            "explain": False,
            "tags": ["customer"],
            "memory_type": "preference",
            "user_id": "alice",
            "session_id": "s-1",
            "run_id": "r-1",
            "agent_id": "bot",
        }
    ]
    assert found.passages == ["Alice prefers aisle seats"]
    assert found.sources == ["support/prefs/seat"]
    assert found.scores == [0.8765]
    assert found.metadata == [
        {
            "namespace": "support",
            "memory_key": "prefs/seat",
            "memory_type": "semantic",
            "similarity": 0.8765,
            "source": "fact",
        }
    ]


def test_memory_metadata_has_valid_at_only_when_the_row_has_one() -> None:
    memory = _SyncSearch(_memories([_memory("k", "text", valid_at="2026-11-01T00:00:00Z")]))
    (metadata,) = LoomMemoryRetriever(_sync_agent(memory=memory))("q").metadata
    assert metadata["valid_at"] == "2026-11-01T00:00:00Z"


def test_memory_abstain_returns_nothing_unless_disabled() -> None:
    weak = _memories([_memory("k", "a weak match", 0.2)], abstain=True)
    strict = LoomMemoryRetriever(_sync_agent(memory=_SyncSearch(weak)))
    assert strict("anything").passages == []
    loose = LoomMemoryRetriever(_sync_agent(memory=_SyncSearch(weak)), honour_abstain=False)
    assert loose("anything").sources == ["support/k"]


def test_memory_below_the_floor_is_left_out_unless_disabled() -> None:
    weak = dataclasses.replace(_memory("k2", "a weak match", 0.2), below_threshold=True)
    # three rows for k=2: the floor is applied before the cut
    result = _memories([_memory("k1", "aisle seats", 0.9), weak, _memory("k3", "vegetarian", 0.8)])
    strict = LoomMemoryRetriever(_sync_agent(memory=_SyncSearch(result)), k=2)
    assert strict("seat").sources == ["support/k1", "support/k3"]
    loose = LoomMemoryRetriever(_sync_agent(memory=_SyncSearch(result)), k=2, honour_abstain=False)
    assert loose("seat").sources == ["support/k1", "support/k2"]


def test_a_call_on_an_async_agent_says_to_await_acall() -> None:
    retriever = LoomMemoryRetriever(_agent(memory=_FakeSearch(_memories([]))))
    with pytest.raises(TypeError, match="acall"):
        retriever("q")


def test_acall_runs_on_an_async_agent_and_on_a_sync_agent() -> None:
    knowledge = _FakeSearch([_chunk("doc://a", 0, "text")])
    found = asyncio.run(LoomKnowledgeRetriever(_agent(knowledge=knowledge)).acall("q", k=1))
    assert found.sources == ["support/doc://a#0"]
    assert knowledge.calls[0]["top_k"] == 1
    sync = _SyncSearch([_chunk("doc://a", 0, "text")])
    # acall on a SyncAgent runs the blocking call off the event loop
    found = asyncio.run(LoomKnowledgeRetriever(_sync_agent(knowledge=sync)).acall("q"))
    assert found.sources == ["support/doc://a#0"]


def test_a_retriever_works_inside_a_dspy_module() -> None:
    class Context(dspy.Module):
        def __init__(self, search: LoomKnowledgeRetriever) -> None:
            super().__init__()
            self.search = search

        def forward(self, question: str) -> dspy.Prediction:
            found = self.search(question)
            return dspy.Prediction(context=found.passages, cited=found.sources)

    knowledge = _SyncSearch([_chunk("doc://refunds", 0, "Annual plans refund pro rata.")])
    out = Context(LoomKnowledgeRetriever(_sync_agent(knowledge=knowledge)))(question="refunds?")
    assert out.context == ["Annual plans refund pro rata."]
    assert out.cited == ["support/doc://refunds#0"]


def test_as_tool_gives_react_one_cited_line_per_hit() -> None:
    knowledge = _SyncSearch([_chunk("doc://refunds", 0, "Annual plans refund pro rata.")])
    tool = LoomKnowledgeRetriever(_sync_agent(knowledge=knowledge), k=2).as_tool()
    assert isinstance(tool, dspy.Tool)
    assert tool.name == "loom_search_documents"
    assert "[source]" in tool.desc
    assert tool.args == {"query": {"type": "string"}}
    assert tool(query="refunds") == ["[support/doc://refunds#0] Annual plans refund pro rata."]


def test_react_calls_the_tool_and_sees_the_cited_passages() -> None:
    knowledge = _SyncSearch([_chunk("doc://refunds", 0, "Annual plans refund pro rata.")])
    tool = LoomKnowledgeRetriever(_sync_agent(knowledge=knowledge)).as_tool()
    # A scripted LM: one turn calls the Loom tool, the next submits the answer.
    lm = DummyLM(
        [
            {
                "next_thought": "look it up",
                "tool_calls": {
                    "tool_calls": [{"name": "loom_search_documents", "args": {"query": "refund"}}]
                },
            },
            {
                "next_thought": "answer",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "Pro rata."}}]},
            },
        ]
    )
    with dspy.context(lm=lm):
        out = dspy.ReAct("question -> answer", tools=[tool])(question="Do annual plans refund?")
    assert out.answer == "Pro rata."
    assert [call["query"] for call in knowledge.calls] == ["refund"]
    first = out.history.messages[0]["tool_calls"].tool_call_results.tool_call_results[0]
    assert (first.name, first.is_error) == ("loom_search_documents", False)
    assert first.value == ["[support/doc://refunds#0] Annual plans refund pro rata."]


def test_as_tool_on_an_async_agent_is_async_and_can_be_renamed() -> None:
    memory = _FakeSearch(_memories([_memory("prefs/seat", "Alice prefers aisle seats")]))
    tool = LoomMemoryRetriever(_agent(memory=memory)).as_tool(name="recall")
    assert tool.name == "recall"
    assert asyncio.run(tool.acall(query="seat")) == [
        "[support/prefs/seat] Alice prefers aisle seats"
    ]


def test_without_dspy_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "dspy", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.dspy")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[dspy\]"):
        importlib.import_module("loom.integrations.dspy")


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
        _real_agent(wire), k=2, search_tags=["policy"], uri_prefix=["doc://"]
    )
    found = asyncio.run(retriever.acall("refunds"))
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
    assert found.sources == ["support/doc://refunds#0"]


def test_the_memory_retriever_posts_memory_search_through_the_sdk() -> None:
    wire = _Wire({"results": [_MEMORY]})
    retriever = LoomMemoryRetriever(_real_agent(wire), k=2, user_id="alice")
    found = asyncio.run(retriever.acall("seat"))
    ((path, body),) = wire.posts
    assert path == "/v1/memory.search"
    assert {k: body[k] for k in ("query", "top_k", "bodies", "explain", "user_id")} == {
        "query": "seat",
        "top_k": 2,
        "bodies": "full",
        "explain": False,
        "user_id": "alice",
    }
    assert found.sources == ["support/prefs/seat"]


def test_a_call_on_a_real_sync_agent_posts_through_its_loop() -> None:
    wire = _Wire({"results": [_CHUNK], "embedding_model": "m"})
    with SyncAgent(
        org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k"
    ) as sa:
        sa._agent.transport.post = wire.post  # type: ignore[method-assign]
        search = LoomKnowledgeRetriever(sa)
        assert search("refunds").sources == asyncio.run(search.acall("refunds")).sources
    assert [p for p, _ in wire.posts] == ["/v1/knowledge.search", "/v1/knowledge.search"]


# Session protection, as the other retrievers keep it: contributors bound or ambient
# at construction stay with the retriever after the agent is rebound.


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
            yield agent
        finally:
            if isinstance(agent, SyncAgent):
                agent.close()
            else:
                asyncio.run(agent.close())


def _native_call(retriever: LoomKnowledgeRetriever | LoomMemoryRetriever, mode: str) -> Any:
    if mode == "call":
        return retriever("seat")
    return asyncio.run(retriever.acall("seat"))


@pytest.mark.parametrize("kind", ["knowledge", "memory"])
@pytest.mark.parametrize("mode", ["async", "call", "thread"])
@pytest.mark.parametrize("constructor", ["bound", "ambient"])
def test_automatic_retrieval_retains_constructor_contributors(
    tmp_path: Path, kind: str, mode: str, constructor: str
) -> None:
    bound = SessionProtection.create(base=tmp_path / "bound")
    ambient = SessionProtection.create(base=tmp_path / "ambient")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        hit = _CHUNK if request.url.path == "/v1/knowledge.search" else _MEMORY
        return httpx.Response(200, json={"results": [hit]})

    with _native_agent(mode, None if constructor == "ambient" else bound, respond) as agent:
        with observation_scope(() if constructor == "bound" else (ambient,)):
            retriever: LoomKnowledgeRetriever | LoomMemoryRetriever = (
                LoomKnowledgeRetriever(agent) if kind == "knowledge" else LoomMemoryRetriever(agent)
            )
        agent.transport.session_protection = current
        (bound if constructor == "bound" else ambient).mark()
        with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            _native_call(retriever, mode)
        assert posts == []
        assert not current.protected and not producer.protected


@pytest.mark.parametrize("mode", ["async", "call"])
def test_explicit_retrieval_stays_explicit_and_returns_passages(tmp_path: Path, mode: str) -> None:
    family = SessionProtection.create(base=tmp_path / "family")

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_CHUNK], "embedding_model": "m"})

    with _native_agent(mode, family, respond) as agent:
        found = _native_call(LoomKnowledgeRetriever(agent), mode)
    assert found.passages == ["Annual plans refund pro rata."]
    assert not family.protected
