"""The LangChain retrievers against a running Loom: a document and a memory
written through the SDK come back as LangChain Documents."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from loom import SyncAgent

# An SDK extra, outside the server's environment: install langchain-core to run these.
pytest.importorskip("langchain_core")

from loom.integrations.langchain import (
    LoomKnowledgeRetriever,
    LoomMemoryRetriever,
)


async def test_knowledge_retriever_returns_the_ingested_chunk(agent) -> None:
    uri = f"doc://refunds-{uuid.uuid4().hex[:6]}"
    await agent.knowledge.add(
        content="Annual plans are refunded pro rata within thirty days of renewal.",
        source_uri=uri,
        title="Refunds",
    )
    retriever = LoomKnowledgeRetriever(agent=agent, k=3, uri_prefix=[uri])
    docs = await retriever.ainvoke("annual plan refund window")
    assert [d.metadata["source_uri"] for d in docs] == [uri]
    assert "pro rata" in docs[0].page_content
    assert docs[0].id == f"{agent.namespace}/{uri}#0"


async def test_memory_retriever_returns_the_written_memory(agent) -> None:
    await agent.memory.set("prefs/seat", "Alice prefers aisle seats on long flights")
    retriever = LoomMemoryRetriever(agent=agent, honour_abstain=False)
    for _ in range(20):  # the write is searchable once its embedding lands
        docs = await retriever.ainvoke("Alice aisle seats long flights")
        if docs:
            break
        await asyncio.sleep(0.25)
    assert [d.metadata["memory_key"] for d in docs][:1] == ["prefs/seat"]


def test_invoke_on_a_sync_agent(control_plane_url: str) -> None:
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
        docs = LoomKnowledgeRetriever(agent=sync_agent, uri_prefix=[uri]).invoke("laptop refresh")
    assert [d.id for d in docs] == [f"{namespace}/{uri}#0"]
