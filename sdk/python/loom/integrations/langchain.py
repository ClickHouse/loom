"""Loom as LangChain retrievers.

``LoomKnowledgeRetriever`` searches the documents in a Loom namespace
(``knowledge.search``) and ``LoomMemoryRetriever`` its memories
(``memory.search``). Both are LangChain ``BaseRetriever`` subclasses, so they
run anywhere a retriever does: ``invoke`` / ``ainvoke``, a chain, or
``create_retriever_tool`` for an agent.

Usage::

    from loom import Agent, SyncAgent
    from loom.integrations.langchain import LoomKnowledgeRetriever

    retriever = LoomKnowledgeRetriever(agent=SyncAgent(org="acme", namespace="support"))
    docs = retriever.invoke("refund policy for annual plans")

    async with Agent(org="acme", namespace="support") as agent:
        docs = await LoomKnowledgeRetriever(agent=agent).ainvoke("refund policy")

Pass a ``SyncAgent`` for ``invoke`` and an ``Agent`` for ``ainvoke``; a
``SyncAgent`` also serves ``ainvoke``, from a worker thread. Identity and
isolation come from the agent: the retriever reads only what its token may
read. Install with ``pip install 'clickhouse-loom[langchain]'``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

try:
    from langchain_core.callbacks import (
        AsyncCallbackManagerForRetrieverRun,
        CallbackManagerForRetrieverRun,
    )
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.langchain needs langchain-core: pip install 'clickhouse-loom[langchain]'"
    ) from e

from loom.sync import SyncAgent


class _LoomRetriever(BaseRetriever):
    agent: Any
    """A ``loom.Agent`` (for ``ainvoke``) or a ``loom.SyncAgent`` (for both)."""
    k: int = 4
    """Documents per query."""
    search_tags: list[str] | None = None
    """Loom tags every returned row must carry. (``tags`` is LangChain's
    callback tags field.)"""

    _client: str  # the agent's client the search runs on

    def _call_kwargs(self) -> dict[str, Any]:
        raise NotImplementedError

    def _documents(self, result: Any) -> list[Document]:
        raise NotImplementedError

    def _sync_search(self, query: str) -> list[Document]:
        client = getattr(self.agent, self._client)
        return self._documents(client.search(query, **self._call_kwargs()))

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        if not isinstance(self.agent, SyncAgent):
            raise TypeError(
                f"{type(self).__name__}.invoke needs a loom.SyncAgent; "
                "with a loom.Agent, call ainvoke"
            )
        return self._sync_search(query)

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        if isinstance(self.agent, SyncAgent):
            # A SyncAgent blocks; its own loop thread serves the call.
            return await asyncio.to_thread(self._sync_search, query)
        client = getattr(self.agent, self._client)
        return self._documents(await client.search(query, **self._call_kwargs()))


class LoomKnowledgeRetriever(_LoomRetriever):
    """Document chunks from ``knowledge.search``, one ``Document`` per chunk.

    ``page_content`` is the chunk text; ``metadata`` carries ``source_uri``,
    ``chunk_idx``, ``similarity``, ``section``, ``namespace`` and ``tags``, and
    the document id is ``<source_uri>#<chunk_idx>``.
    """

    _client = "knowledge"
    uri_prefix: list[str] | None = None
    """Only documents whose ``source_uri`` starts with one of these."""
    namespace_scope: Literal["this", "all_mine"] = "this"
    """``all_mine`` searches every namespace the token may read."""
    search_kwargs: dict[str, Any] = {}
    """Further ``knowledge.search`` fields (``search_mode``, ``rerank``, ...)."""

    def _call_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {**self.search_kwargs, "top_k": self.k}
        if self.search_tags:
            kwargs["tags"] = list(self.search_tags)
        if self.uri_prefix:
            kwargs["uri_prefix"] = list(self.uri_prefix)
        if self.namespace_scope != "this":
            kwargs["namespace_scope"] = self.namespace_scope
        return kwargs

    def _documents(self, result: Any) -> list[Document]:
        return [
            Document(
                page_content=hit.content,
                metadata={
                    "source_uri": hit.source_uri,
                    "chunk_idx": hit.chunk_idx,
                    "similarity": round(float(hit.similarity), 4),
                    "section": hit.section,
                    "namespace": hit.namespace,
                    "tags": list(hit.tags),
                },
                id=f"{hit.source_uri}#{hit.chunk_idx}",
            )
            for hit in result
        ]


class LoomMemoryRetriever(_LoomRetriever):
    """Memories from ``memory.search``, one ``Document`` per memory.

    ``page_content`` is the memory's text; ``metadata`` carries
    ``memory_key``, ``memory_type``, ``similarity``, ``source`` and, when set,
    ``valid_at``, and the document id is the memory key. When Loom abstains
    (no memory above its calibrated floor) the retriever returns no documents
    unless ``honour_abstain`` is false.
    """

    _client = "memory"
    memory_type: Literal["semantic", "episodic", "procedural"] | None = None
    """Only memories of this type."""
    user_id: str | None = None
    """The end user whose memories to read."""
    honour_abstain: bool = True

    def _call_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"top_k": self.k}
        if self.search_tags:
            kwargs["tags"] = list(self.search_tags)
        if self.memory_type:
            kwargs["memory_type"] = self.memory_type
        if self.user_id:
            kwargs["user_id"] = self.user_id
        return kwargs

    def _documents(self, result: Any) -> list[Document]:
        if self.honour_abstain and getattr(result, "abstain", False):
            return []
        docs = []
        for hit in list(result)[: self.k]:
            metadata: dict[str, Any] = {
                "memory_key": hit.memory_key,
                "memory_type": hit.memory_type,
                "similarity": round(float(hit.similarity), 4),
                "source": hit.source,
            }
            if hit.valid_at:
                metadata["valid_at"] = hit.valid_at
            docs.append(
                Document(page_content=hit.content_excerpt, metadata=metadata, id=hit.memory_key)
            )
        return docs
