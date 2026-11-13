"""LoomRetriever and LoomMemoryBlock: LlamaIndex's BaseRetriever and
BaseMemoryBlock over fake Loom clients, and a real Agent with its HTTP post
stubbed: the fields each call sends, the nodes and text each returns,
abstention, and which agent serves the sync and async entry points."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.base.llms.types import ChatMessage
from llama_index.core.memory import Memory
from llama_index.core.memory.memory import BaseMemoryBlock
from llama_index.core.schema import MetadataMode
from llama_index.core.storage.chat_store.base_db import AsyncDBChatStore, MessageStatus
from loom import Agent
from loom.integrations.llamaindex import LoomMemoryBlock, LoomRetriever, aflush, flush
from loom.knowledge import SearchHit
from loom.memory import MemorySearchHit, MemorySearchResult
from loom.sync import SyncAgent


class _ListStore(AsyncDBChatStore):
    """Memory's chat store held in lists: the tests' Memory starts no
    database connection, so nothing outlives the loop a test ran on."""

    rows: dict[str, list[tuple[ChatMessage, MessageStatus]]] = {}

    def _of(self, key: str, status: MessageStatus | None) -> list[ChatMessage]:
        # Copies, as a database store rebuilds each message on every read.
        rows = self.rows.get(key, [])
        return [m.model_copy(deep=True) for m, st in rows if status is None or st == status]

    async def get_messages(
        self,
        key: str,
        status: MessageStatus | None = MessageStatus.ACTIVE,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[ChatMessage]:
        found = self._of(key, status)[offset or 0 :]
        return found[:limit] if limit is not None else found

    async def count_messages(
        self, key: str, status: MessageStatus | None = MessageStatus.ACTIVE
    ) -> int:
        return len(self._of(key, status))

    async def add_message(
        self, key: str, message: ChatMessage, status: MessageStatus = MessageStatus.ACTIVE
    ) -> None:
        self.rows.setdefault(key, []).append((message, status))

    async def add_messages(
        self, key: str, messages: list[ChatMessage], status: MessageStatus = MessageStatus.ACTIVE
    ) -> None:
        for message in messages:
            await self.add_message(key, message, status)

    async def set_messages(
        self, key: str, messages: list[ChatMessage], status: MessageStatus = MessageStatus.ACTIVE
    ) -> None:
        self.rows[key] = [(m, status) for m in messages]

    async def delete_message(self, key: str, idx: int) -> ChatMessage | None:
        raise NotImplementedError  # Memory does not call it

    async def delete_messages(self, key: str, status: MessageStatus | None = None) -> None:
        self.rows[key] = [
            (m, st) for m, st in self.rows.get(key, []) if status is not None and st != status
        ]

    async def delete_oldest_messages(self, key: str, n: int) -> list[ChatMessage]:
        raise NotImplementedError  # Memory does not call it

    async def archive_oldest_messages(self, key: str, n: int) -> list[ChatMessage]:
        archived: list[ChatMessage] = []
        rows = self.rows.get(key, [])
        for i, (m, st) in enumerate(rows):
            if len(archived) == n:
                break
            if st == MessageStatus.ACTIVE:
                rows[i] = (m, MessageStatus.ARCHIVED)
                archived.append(m)
        return archived

    async def get_keys(self) -> list[str]:
        return list(self.rows)


def _new_memory(**kw: Any) -> Memory:
    memory = Memory.from_defaults(**kw)
    # The default store connects on first use; swap it before then (0.14.0 has
    # no chat_store argument).
    memory.sql_store = _ListStore()
    return memory


class _FakeKnowledge:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._hits = hits

    async def search(self, query: str, **kw: Any) -> list[SearchHit]:
        self.calls.append({"query": query, **kw})
        return self._hits


class _SyncKnowledge(_FakeKnowledge):
    def search(self, query: str, **kw: Any) -> list[SearchHit]:  # type: ignore[override]
        self.calls.append({"query": query, **kw})
        return self._hits


class _FakeMemory:
    def __init__(self, hits: list[MemorySearchHit] | None = None, abstain: bool = False) -> None:
        self.searches: list[dict[str, Any]] = []
        self.extractions: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
        self._hits = hits or []
        self._abstain = abstain

    async def search(self, query: str, **kw: Any) -> MemorySearchResult:
        self.searches.append({"query": query, **kw})
        result = MemorySearchResult(self._hits[: kw.get("top_k", 5)])
        result.abstain = self._abstain
        return result

    async def set_from_messages(self, messages: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        self.extractions.append((messages, kw))
        return {}


def _chunk(uri: str, idx: int, text: str, sim: float = 0.8) -> SearchHit:
    return SearchHit(
        source_uri=uri,
        chunk_idx=idx,
        content=text,
        tags=["policy"],
        distance=1 - sim,
        similarity=sim,
        section="Refunds",
    )


def _memory(key: str, text: str, sim: float = 0.8) -> MemorySearchHit:
    return MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt=text, similarity=sim
    )


def test_both_implement_the_llamaindex_interfaces() -> None:
    assert issubclass(LoomRetriever, BaseRetriever)
    assert issubclass(LoomMemoryBlock, BaseMemoryBlock)


def test_retriever_nodes_carry_the_chunk_score_and_source() -> None:
    knowledge = _FakeKnowledge(
        [_chunk("doc://refunds", 2, "Annual plans refund pro rata.", 0.91234)]
    )
    retriever = LoomRetriever(
        SimpleNamespace(namespace="support", knowledge=knowledge), similarity_top_k=3
    )
    nodes = asyncio.run(retriever.aretrieve("refund policy"))
    # namespace_scope is always sent: the server's own default is all_mine
    assert knowledge.calls == [{"query": "refund policy", "top_k": 3, "namespace_scope": "this"}]
    assert len(nodes) == 1
    assert nodes[0].node.get_content() == "Annual plans refund pro rata."
    assert nodes[0].node_id == "support/doc://refunds#2"
    assert nodes[0].score == 0.91234
    assert nodes[0].node.metadata == {
        "source_uri": "doc://refunds",
        "chunk_idx": 2,
        "section": "Refunds",
        "namespace": "support",
        "tags": ["policy"],
    }


def test_a_fanned_out_hit_is_named_by_its_own_namespace() -> None:
    hit = _chunk("doc://refunds", 0, "text")
    hit = dataclasses.replace(hit, namespace="billing")
    retriever = LoomRetriever(
        SimpleNamespace(namespace="support", knowledge=_FakeKnowledge([hit])),
        namespace_scope="all_mine",
    )
    (node,) = asyncio.run(retriever.aretrieve("refunds"))
    assert node.node_id == "billing/doc://refunds#0"
    assert node.node.metadata["namespace"] == "billing"


def test_retriever_sends_its_filters_and_top_k_wins() -> None:
    knowledge = _FakeKnowledge([])
    retriever = LoomRetriever(
        SimpleNamespace(namespace="support", knowledge=knowledge),
        tags=["policy"],
        uri_prefix=["doc://handbook/"],
        namespace_scope="all_mine",
        search_kwargs={"search_mode": "lexical", "top_k": 99},
    )
    asyncio.run(retriever.aretrieve("refunds"))
    assert knowledge.calls == [
        {
            "query": "refunds",
            "search_mode": "lexical",
            "top_k": 4,
            "tags": ["policy"],
            "uri_prefix": ["doc://handbook/"],
            "namespace_scope": "all_mine",
        }
    ]


def test_retrieve_runs_on_a_sync_agent_and_refuses_an_async_one() -> None:
    agent = object.__new__(SyncAgent)  # no loop or transport behind it
    agent._agent = SimpleNamespace(namespace="support")  # type: ignore[attr-defined]
    agent.knowledge = _SyncKnowledge([_chunk("doc://a", 0, "text")])  # type: ignore[assignment]
    retriever = LoomRetriever(agent)
    assert [n.node_id for n in retriever.retrieve("q")] == ["support/doc://a#0"]
    assert [n.node_id for n in asyncio.run(retriever.aretrieve("q"))] == ["support/doc://a#0"]
    with pytest.raises(TypeError, match="aretrieve"):
        LoomRetriever(SimpleNamespace(knowledge=_FakeKnowledge([]))).retrieve("q")


def test_memory_block_reads_with_the_latest_user_message() -> None:
    memory = _FakeMemory(
        [_memory("k1", "Alice prefers aisle seats"), _memory("k2", "Alice is vegetarian")]
    )
    block = LoomMemoryBlock(
        agent=SimpleNamespace(memory=memory), top_k=1, tags=["customer"], user_id="alice"
    )
    text = asyncio.run(
        block.aget(
            [
                ChatMessage(role="user", content="Book me a flight"),
                ChatMessage(role="assistant", content="Where to?"),
                ChatMessage(role="user", content="Lisbon, and pick my usual seat"),
                ChatMessage(role="assistant", content=""),
            ]
        )
    )
    assert memory.searches == [
        {
            "query": "Lisbon, and pick my usual seat",
            "top_k": 1,
            "namespace_scope": "this",
            "tags": ["customer"],
            "user_id": "alice",
        }
    ]
    assert text == "- Alice prefers aisle seats"


def test_memory_block_reads_nothing_without_a_user_message_or_when_loom_abstains() -> None:
    memory = _FakeMemory([_memory("k", "weak")], abstain=True)
    block = LoomMemoryBlock(agent=SimpleNamespace(memory=memory))
    assert asyncio.run(block.aget([ChatMessage(role="assistant", content="hi")])) == ""
    assert asyncio.run(block.aget(None)) == ""
    assert memory.searches == []
    assert asyncio.run(block.aget([ChatMessage(role="user", content="seat?")])) == ""
    loose = LoomMemoryBlock(agent=SimpleNamespace(memory=memory), honour_abstain=False)
    assert asyncio.run(loose.aget([ChatMessage(role="user", content="seat?")])) == "- weak"


def test_memory_block_sends_the_conversation_turns_for_extraction() -> None:
    memory = _FakeMemory()
    block = LoomMemoryBlock(
        agent=SimpleNamespace(memory=memory), tags=["customer"], user_id="alice"
    )
    asyncio.run(
        block.aput(
            [
                ChatMessage(role="system", content="You are a travel agent"),
                ChatMessage(role="user", content="I always take an aisle seat"),
                ChatMessage(role="assistant", content="Noted"),
                ChatMessage(role="tool", content="{}"),
            ],
            session_id="s-42",
        )
    )
    asyncio.run(block.aput([ChatMessage(role="system", content="only a system message")]))
    assert memory.extractions == [
        (
            [
                {"role": "user", "content": "I always take an aisle seat"},
                {"role": "assistant", "content": "Noted"},
            ],
            {"extra_tags": ["customer"], "user_id": "alice", "session_id": "s-42", "defer": True},
        )
    ]


def test_memory_waterfalls_flushed_messages_into_the_block() -> None:
    memory = _FakeMemory([_memory("k1", "Alice prefers aisle seats")])
    block = LoomMemoryBlock(agent=SimpleNamespace(memory=memory))

    async def run() -> list[ChatMessage]:
        mem = _new_memory(
            session_id="s-1",
            token_limit=40,
            chat_history_token_ratio=0.5,
            token_flush_size=10,
            memory_blocks=[block],
        )
        for i in range(6):
            await mem.aput(
                ChatMessage(role="user", content=f"message {i} about my aisle seat preference")
            )
            await mem.aput(ChatMessage(role="assistant", content=f"reply {i}"))
        return await mem.aget()

    history = asyncio.run(run())
    assert memory.extractions, "Memory flushed nothing to the block"
    assert all(kw["session_id"] == "s-1" for _, kw in memory.extractions)
    assert "Alice prefers aisle seats" in history[0].content


class _SyncMemory(_FakeMemory):
    """A SyncAgent's memory client: blocking calls."""

    def search(self, query: str, **kw: Any) -> MemorySearchResult:  # type: ignore[override]
        self.searches.append({"query": query, **kw})
        result = MemorySearchResult(self._hits[: kw.get("top_k", 5)])
        result.abstain = self._abstain
        return result

    def set_from_messages(self, messages: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:  # type: ignore[override]
        self.extractions.append((messages, kw))
        return {}


def test_memory_sync_methods_run_the_block_on_a_sync_agent() -> None:
    """Memory.put / get may run a call on another loop than an Agent's: a SyncAgent serves them."""
    memory = _SyncMemory([_memory("k1", "Alice prefers aisle seats")])
    agent = object.__new__(SyncAgent)
    agent.memory = memory  # type: ignore[assignment]
    block = LoomMemoryBlock(agent=agent, defer=False)
    mem = _new_memory(
        session_id="s-1",
        token_limit=40,
        chat_history_token_ratio=0.5,
        token_flush_size=10,
        memory_blocks=[block],
    )
    for i in range(6):
        mem.put(ChatMessage(role="user", content=f"message {i} about my aisle seat preference"))
        mem.put(ChatMessage(role="assistant", content=f"reply {i}"))
    history = mem.get()
    assert memory.extractions
    assert all(kw["defer"] is False for _, kw in memory.extractions)
    assert "Alice prefers aisle seats" in history[0].content


def test_without_llama_index_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "llama_index.core.base.base_retriever", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.llamaindex")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[llamaindex\]"):
        importlib.import_module("loom.integrations.llamaindex")


# The SDK's own clients, with only the HTTP boundary stubbed: a renamed client
# argument or a changed request body fails here.


class _Wire:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._reply = reply

    async def post(self, path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        self.posts.append((path, dict(payload)))
        return self._reply


def _real_agent(wire: _Wire) -> Agent:
    agent = Agent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = wire.post  # type: ignore[method-assign]
    return agent


def test_the_retriever_posts_knowledge_search_through_the_sdk() -> None:
    chunk = {
        "source_uri": "doc://refunds",
        "chunk_idx": 0,
        "content": "Annual plans refund pro rata.",
        "tags": [],
        "distance": 0.1,
        "similarity": 0.9,
    }
    wire = _Wire({"results": [chunk], "embedding_model": "m"})
    retriever = LoomRetriever(_real_agent(wire), similarity_top_k=2, tags=["policy"])
    (node,) = asyncio.run(retriever.aretrieve("refunds"))
    ((path, body),) = wire.posts
    assert path == "/v1/knowledge.search"
    assert {k: body[k] for k in ("query", "top_k", "tags", "namespace_scope")} == {
        "query": "refunds",
        "top_k": 2,
        "tags": ["policy"],
        "namespace_scope": "this",
    }
    assert node.node_id == "support/doc://refunds#0"


def test_the_block_posts_memory_search_and_extraction_through_the_sdk() -> None:
    hit = {
        "memory_key": "prefs/seat",
        "memory_type": "preference",
        "content_excerpt": "Alice prefers aisle seats",
        "distance": 0.1,
        "similarity": 0.9,
    }
    wire = _Wire({"results": [hit]})
    block = LoomMemoryBlock(agent=_real_agent(wire), top_k=2, tags=["customer"], user_id="alice")
    text = asyncio.run(block.aget([ChatMessage(role="user", content="seat?")]))
    asyncio.run(
        block.aput([ChatMessage(role="user", content="I like aisle seats")], session_id="s-1")
    )
    (search, extract) = wire.posts
    assert search[0] == "/v1/memory.search"
    assert {k: search[1][k] for k in ("query", "top_k", "namespace_scope", "tags", "user_id")} == {
        "query": "seat?",
        "top_k": 2,
        "namespace_scope": "this",
        "tags": ["customer"],
        "user_id": "alice",
    }
    assert text == "- Alice prefers aisle seats"
    assert extract[0] == "/v1/memory.set_from_messages"
    assert extract[1]["messages"] == [{"role": "user", "content": "I like aisle seats"}]
    assert extract[1]["session_id"] == "s-1"
    assert extract[1]["user_id"] == "alice"
    assert extract[1]["defer"] is True
    assert extract[1]["extra_tags"] == ["customer"]
    inline = LoomMemoryBlock(agent=_real_agent(wire), defer=False)
    asyncio.run(inline.aput([ChatMessage(role="user", content="I like aisle seats")]))
    assert wire.posts[-1][1]["defer"] is False  # sent, so the operator's allowlist cannot queue it


class _Overfull(_FakeMemory):
    """Answers with every hit, as a search whose derived rows sit above its top_k slice."""

    async def search(self, query: str, **kw: Any) -> MemorySearchResult:
        self.searches.append({"query": query, **kw})
        result = MemorySearchResult(self._hits)
        result.abstain = self._abstain
        return result


def test_the_block_leaves_out_rows_below_the_floor_then_caps_at_top_k() -> None:
    weak = dataclasses.replace(_memory("k2", "a weak match"), below_threshold=True)
    hits = [_memory("k1", "Alice prefers aisle seats"), weak, _memory("k3", "Alice is vegetarian")]
    asked = [ChatMessage(role="user", content="seat?")]
    strict = LoomMemoryBlock(agent=SimpleNamespace(memory=_Overfull(hits)), top_k=2)
    assert asyncio.run(strict.aget(asked)) == "- Alice prefers aisle seats\n- Alice is vegetarian"
    loose = LoomMemoryBlock(
        agent=SimpleNamespace(memory=_Overfull(hits)), top_k=2, honour_abstain=False
    )
    assert asyncio.run(loose.aget(asked)) == "- Alice prefers aisle seats\n- a weak match"


def test_a_synthesizer_sees_the_chunk_and_its_source_not_the_bookkeeping() -> None:
    knowledge = _FakeKnowledge([_chunk("doc://refunds", 2, "Annual plans refund pro rata.")])
    agent = SimpleNamespace(namespace="support", knowledge=knowledge)
    (node,) = asyncio.run(LoomRetriever(agent).aretrieve("refunds"))
    text = node.node.get_content(metadata_mode=MetadataMode.LLM)
    assert "Annual plans refund pro rata." in text
    assert "doc://refunds" in text
    assert "Refunds" in text
    for key in ("chunk_idx", "namespace", "tags"):
        assert key not in text


def test_a_chunk_without_a_section_sends_no_section_line() -> None:
    hit = dataclasses.replace(_chunk("doc://notes", 0, "Plain text."), section="")
    agent = SimpleNamespace(namespace="support", knowledge=_FakeKnowledge([hit]))
    (node,) = asyncio.run(LoomRetriever(agent).aretrieve("notes"))
    assert "section" not in node.node.metadata
    assert "section" not in node.node.get_content(metadata_mode=MetadataMode.LLM)


def test_flush_sends_what_is_still_in_the_window() -> None:
    memory = _FakeMemory()
    block = LoomMemoryBlock(agent=SimpleNamespace(memory=memory))

    async def run() -> None:
        mem = _new_memory(session_id="s-9", memory_blocks=[block])
        await mem.aput(ChatMessage(role="user", content="I always take an aisle seat"))
        await mem.aput(ChatMessage(role="assistant", content="Noted"))
        assert memory.extractions == []  # well under the window: nothing flushed yet
        await aflush(mem)

    asyncio.run(run())
    ((turns, kw),) = memory.extractions
    assert [t["content"] for t in turns] == ["I always take an aisle seat", "Noted"]
    assert kw["session_id"] == "s-9"


def test_a_real_sync_agent_serves_memory_sync_methods_and_retrieve() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []
    hit = {
        "memory_key": "prefs/seat",
        "memory_type": "preference",
        "content_excerpt": "Alice prefers aisle seats",
        "distance": 0.1,
        "similarity": 0.9,
    }
    chunk = {
        "source_uri": "doc://refunds",
        "chunk_idx": 0,
        "content": "Annual plans refund pro rata.",
        "tags": [],
        "distance": 0.1,
        "similarity": 0.9,
    }

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        if path == "/v1/knowledge.search":
            return {"results": [chunk], "embedding_model": "m"}
        return {"results": [hit]}

    sync = SyncAgent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k")
    with sync as sa:
        sa._agent.transport.post = post  # type: ignore[method-assign]
        block = LoomMemoryBlock(agent=sa, defer=False)
        mem = _new_memory(session_id="s-3", memory_blocks=[block])
        mem.put(ChatMessage(role="user", content="seat?"))
        history = mem.get()
        flush(mem)
        (node,) = LoomRetriever(sa).retrieve("refunds")
    assert history[0].role == "system"
    assert "Alice prefers aisle seats" in history[0].content
    assert node.node_id == "support/doc://refunds#0"
    assert [p for p, _ in posts] == [
        "/v1/memory.search",
        "/v1/memory.set_from_messages",
        "/v1/knowledge.search",
    ]


def test_a_flushed_turn_is_not_sent_again() -> None:
    memory = _FakeMemory()
    block = LoomMemoryBlock(agent=SimpleNamespace(memory=memory))

    async def run() -> None:
        mem = _new_memory(
            session_id="s-8",
            token_limit=40,
            chat_history_token_ratio=0.5,
            token_flush_size=10,
            memory_blocks=[block],
        )
        await mem.aput(ChatMessage(role="user", content="first turn"))
        await aflush(mem)
        await aflush(mem)  # nothing left in the window
        for i in range(6):  # overflow hands later turns over, not the flushed one
            await mem.aput(ChatMessage(role="user", content=f"later turn {i} about seats"))
        await aflush(mem)

    asyncio.run(run())
    sent = [t["content"] for turns, _ in memory.extractions for t in turns]
    assert sent.count("first turn") == 1
    assert sorted(set(sent)) == sorted(sent)  # every turn once


def test_flush_on_an_async_agent_says_to_use_aflush() -> None:
    block = LoomMemoryBlock(agent=SimpleNamespace(memory=_FakeMemory()))
    with pytest.raises(TypeError, match="aflush"):
        flush(_new_memory(session_id="s-7", memory_blocks=[block]))


class _Recorder(BaseMemoryBlock[str]):
    """Another block in the same Memory: records what it is handed."""

    name: str = "recorder"
    seen: list[str] = []

    async def _aget(self, messages: list[ChatMessage] | None = None, **kw: Any) -> str:
        return ""

    async def _aput(self, messages: list[ChatMessage]) -> None:
        self.seen.extend(str(m.content) for m in messages)


def test_flush_hands_the_window_to_every_block_then_empties_it() -> None:
    memory = _FakeMemory()
    loom_block = LoomMemoryBlock(agent=SimpleNamespace(memory=memory))
    other = _Recorder()

    async def run() -> list[ChatMessage]:
        mem = _new_memory(session_id="s-6", memory_blocks=[loom_block, other])
        await mem.aput(ChatMessage(role="user", content="I always take an aisle seat"))
        await aflush(mem)
        return await mem.aget_all(status=MessageStatus.ACTIVE)

    assert asyncio.run(run()) == []
    assert other.seen == ["I always take an aisle seat"]
    assert [t["content"] for turns, _ in memory.extractions for t in turns] == [
        "I always take an aisle seat"
    ]


class _Failing(_Recorder):
    async def _aput(self, messages: list[ChatMessage]) -> None:
        raise RuntimeError("provider down")


class _Slow(_Recorder):
    """Records only after yielding to the loop a few times."""

    async def _aput(self, messages: list[ChatMessage]) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        self.seen.extend(str(m.content) for m in messages)


def test_a_failed_flush_raises_only_after_every_block_finished() -> None:
    slow = _Slow(name="slow")

    async def run() -> None:
        mem = _new_memory(session_id="s-2", memory_blocks=[_Failing(name="failing"), slow])
        await mem.aput(ChatMessage(role="user", content="I always take an aisle seat"))
        with pytest.raises(RuntimeError):
            await aflush(mem)
        assert slow.seen == ["I always take an aisle seat"]  # finished before the raise

    asyncio.run(run())


def test_a_failed_flush_archives_nothing_and_the_next_one_hands_the_window_over_again() -> None:
    memory = _FakeMemory()
    loom_block = LoomMemoryBlock(agent=SimpleNamespace(memory=memory))
    failing = _Failing(name="failing")

    async def run() -> tuple[int, int]:
        mem = _new_memory(session_id="s-5", memory_blocks=[loom_block, failing])
        await mem.aput(ChatMessage(role="user", content="I always take an aisle seat"))
        with pytest.raises(RuntimeError):
            await aflush(mem)
        still_active = len(await mem.aget_all(status=MessageStatus.ACTIVE))
        mem.memory_blocks = [loom_block]  # the failing block is fixed or removed
        await aflush(mem)
        return still_active, len(await mem.aget_all(status=MessageStatus.ACTIVE))

    assert asyncio.run(run()) == (1, 0)
    sent = [t["content"] for turns, _ in memory.extractions for t in turns]
    assert sent == ["I always take an aisle seat", "I always take an aisle seat"]  # at least once


def test_flush_refuses_a_memory_with_any_loom_block_on_an_async_agent() -> None:
    sync = object.__new__(SyncAgent)
    sync.memory = _SyncMemory()  # type: ignore[assignment]
    on_sync = LoomMemoryBlock(name="a", agent=sync)
    on_async = LoomMemoryBlock(
        name="b",
        agent=Agent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k"),
    )
    mem = _new_memory(session_id="s-4", memory_blocks=[on_sync, on_async])
    with pytest.raises(TypeError, match="aflush"):
        flush(mem)
