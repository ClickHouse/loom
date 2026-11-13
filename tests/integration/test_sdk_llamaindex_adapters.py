"""The LlamaIndex retriever and memory block against a running Loom: an added
document comes back as a node, and a conversation put into the block is
extracted and read back."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from loom import SyncAgent

# An SDK extra, outside the server's environment: install llama-index-core to run these.
pytest.importorskip("llama_index.core")

from llama_index.core.base.llms.types import ChatMessage
from llama_index.core.memory import Memory
from llama_index.core.storage.chat_store.base_db import MessageStatus
from loom.integrations.llamaindex import LoomMemoryBlock, LoomRetriever, flush


async def test_retriever_returns_the_added_chunk(agent) -> None:
    uri = f"doc://refunds-{uuid.uuid4().hex[:6]}"
    await agent.knowledge.add(
        content="Annual plans are refunded pro rata within thirty days of renewal.",
        source_uri=uri,
        title="Refunds",
    )
    nodes = await LoomRetriever(agent, uri_prefix=[uri]).aretrieve("annual plan refund window")
    assert [n.node_id for n in nodes] == [f"{agent.namespace}/{uri}#0"]
    assert "pro rata" in nodes[0].node.get_content()


def test_retrieve_on_a_sync_agent(control_plane_url: str) -> None:
    uri = f"doc://handbook-{uuid.uuid4().hex[:6]}"
    namespace = f"test-{uuid.uuid4().hex[:8]}"
    with SyncAgent(
        org="test-org",
        namespace=namespace,
        agent="test-agent",
        base_url=control_plane_url,
        api_key=os.environ["LOOM_API_KEY"],
    ) as sync_agent:
        sync_agent.knowledge.add(
            content="Laptops are replaced every three years.", source_uri=uri, title="Hardware"
        )
        nodes = LoomRetriever(sync_agent, uri_prefix=[uri]).retrieve("laptop refresh")
    assert [n.node_id for n in nodes] == [f"{namespace}/{uri}#0"]


async def test_memory_block_extracts_and_reads_back(agent) -> None:
    # defer=False: extraction inline, since this server runs no background workers
    block = LoomMemoryBlock(agent=agent, honour_abstain=False, defer=False)
    await block.aput(
        [
            ChatMessage(role="user", content="I always book an aisle seat on long flights."),
            ChatMessage(role="assistant", content="Noted, aisle seats from now on."),
        ],
        session_id="s-1",
    )
    text = ""
    for _ in range(20):  # extracted memories are searchable once embedded
        text = await block.aget([ChatMessage(role="user", content="aisle seat long flights")])
        if text:
            break
        await asyncio.sleep(0.25)
    assert "aisle" in text.lower()


def test_memory_sync_methods_with_a_sync_agent(control_plane_url: str) -> None:
    """Memory.put / get may run a block call on another loop than the one an
    Agent was opened on; a SyncAgent's own loop thread serves every one."""
    with SyncAgent(
        org="test-org",
        namespace=f"test-{uuid.uuid4().hex[:8]}",
        agent="test-agent",
        base_url=control_plane_url,
        api_key=os.environ["LOOM_API_KEY"],
    ) as sync_agent:
        block = LoomMemoryBlock(agent=sync_agent, honour_abstain=False, defer=False)
        memory = Memory.from_defaults(
            session_id="s-2",
            token_limit=40,
            chat_history_token_ratio=0.5,
            token_flush_size=10,
            memory_blocks=[block],
        )
        for i in range(4):
            memory.put(ChatMessage(role="user", content=f"I always book an aisle seat, trip {i}."))
            memory.put(ChatMessage(role="assistant", content="Noted."))
        memory.put(ChatMessage(role="user", content="aisle seat"))
        for _ in range(20):  # extracted memories are searchable once embedded
            history = memory.get()
            # Memory puts the blocks' content in a system message only when a block returns some
            if history[0].role == "system":
                break
            time.sleep(0.25)
        flush(memory)  # LlamaIndex's own chat store: the window is archived after the flush
        still_active = memory.get_all(status=MessageStatus.ACTIVE)
    assert history[0].role == "system"
    assert "<LoomMemories>" in history[0].content
    assert "aisle" in history[0].content.lower()
    assert still_active == []
