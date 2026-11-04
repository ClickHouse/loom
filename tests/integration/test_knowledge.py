"""Knowledge plane end-to-end:
  - add → chunks land in CH with embeddings (fake-ngram)
  - search → hybrid (vector + tags + recency) returns ranked hits
  - has_similar → semantic dedup the canonical way

The fake embedder is deterministic and shares trigrams between similar
texts, so dedup actually exhibits the expected behavior on synthetic
findings. A real pipeline would use OpenAI text-embedding-3-small.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_knowledge_add_returns_metadata(agent) -> None:
    """Adding a document returns doc_id + embedding model info."""
    result = await agent.knowledge.add(
        "JOIN performance regression in vectorized aggregation",
        source_uri="github://pr/100",
        tags=["aurora", "regression"],
    )
    assert "doc_id" in result
    assert result["chunks_created"] == 1
    # Don't pin to a specific embedder: prod uses OpenAI
    # text-embedding-3-small (1536d); dev can swap to a fake-ngram one for
    # deterministic offline runs. Just assert the shape.
    assert isinstance(result["embedding_model"], str) and result["embedding_model"]
    assert isinstance(result["embedding_dim"], int) and result["embedding_dim"] > 0


@pytest.mark.asyncio
async def test_knowledge_search_returns_added_document(agent) -> None:
    """Adding then searching for similar text returns the added doc."""
    await agent.knowledge.add(
        "Possible NULL pointer dereference in retry loop on line 42",
        source_uri="github://pr/101",
        tags=["null-deref"],
    )
    hits = await agent.knowledge.search("NULL pointer dereference retry loop", top_k=5)
    assert len(hits) >= 1
    assert hits[0].source_uri == "github://pr/101"
    assert hits[0].similarity > 0.5  # similar text → high similarity


@pytest.mark.asyncio
async def test_knowledge_search_orders_by_similarity(agent) -> None:
    """Most-similar document ranks first."""
    await agent.knowledge.add(
        "Memory leak in materialized view background merge",
        source_uri="github://pr/200",
    )
    await agent.knowledge.add(
        "Authentication bypass in REST handler",
        source_uri="github://pr/201",
    )
    hits = await agent.knowledge.search(
        "materialized view background merge memory leak",
        top_k=2,
    )
    assert len(hits) == 2
    assert hits[0].source_uri == "github://pr/200"  # closer to the query
    assert hits[0].distance < hits[1].distance


@pytest.mark.asyncio
async def test_knowledge_search_filters_by_tags(agent) -> None:
    """`tags=` only returns chunks whose tags intersect."""
    await agent.knowledge.add(
        "Common content phrase that both findings will share completely",
        source_uri="github://pr/300",
        tags=["null-deref"],
    )
    await agent.knowledge.add(
        "Common content phrase that both findings will share completely",
        source_uri="github://pr/301",
        tags=["retry-loop"],
    )
    hits = await agent.knowledge.search(
        "Common content phrase that both findings will share completely",
        tags=["null-deref"],
    )
    uris = {h.source_uri for h in hits}
    assert "github://pr/300" in uris
    assert "github://pr/301" not in uris


@pytest.mark.asyncio
async def test_knowledge_search_names_the_reranker_and_refuses_a_url(agent) -> None:
    """`rerank="none"` reports no reranker; a URL is never a reranker a caller may name."""
    from loom.errors import ValidationError

    await agent.knowledge.add("Refunds are available within 14 days.", source_uri="doc://r")
    body = {"org": agent.org, "namespace": agent.namespace, "query": "refunds", "top_k": 3}
    out = await agent.transport.post("/v1/knowledge.search", {**body, "rerank": "none"})
    assert out["results"] and out["reranked_by"] == ""
    with pytest.raises(ValidationError):
        await agent.transport.post(
            "/v1/knowledge.search", {**body, "rerank": "https://example.com/rerank"}
        )
