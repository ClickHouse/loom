"""The LlamaIndex retriever and memory block on a loom.Agent against a running
Loom: an added document comes back as a node, and a conversation put into the
block is extracted and read back. The SyncAgent cases are in
test_sdk_llamaindex_sync_agent.py."""

from __future__ import annotations

import asyncio
import uuid

import pytest

# An SDK extra, outside the server's environment: install llama-index-core to run these.
pytest.importorskip("llama_index.core")

from llama_index.core.base.llms.types import ChatMessage
from loom.integrations.llamaindex import LoomMemoryBlock, LoomRetriever


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
