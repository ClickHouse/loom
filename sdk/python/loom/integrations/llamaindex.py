"""Loom as a LlamaIndex retriever and memory block.

``LoomRetriever`` searches the documents in a Loom namespace
(``knowledge.search``) as a LlamaIndex ``BaseRetriever``, so it plugs into a
query engine, a chat engine or a retriever tool. ``LoomMemoryBlock`` is a
long-term memory block for LlamaIndex's ``Memory``: messages that leave the
short-term window go to ``memory.set_from_messages``, where Loom extracts the
facts worth keeping, and each turn reads back the memories that match the
latest user message (``memory.search``).

Usage::

    from llama_index.core.memory import Memory
    from loom import SyncAgent
    from loom.integrations.llamaindex import LoomMemoryBlock, LoomRetriever, flush

    with SyncAgent(org="acme", namespace="support") as agent:
        nodes = LoomRetriever(agent).retrieve("refund policy")
        memory = Memory.from_defaults(
            session_id="s-42", memory_blocks=[LoomMemoryBlock(agent=agent)]
        )
        ...  # the session's turns go through memory.put / memory.get
        flush(memory)  # at the session's end

Identity and isolation come from the Loom agent: the retriever and the block
read only what its token may read. Install with
``pip install 'clickhouse-loom[llamaindex]'``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

try:
    from llama_index.core.async_utils import asyncio_run
    from llama_index.core.base.base_retriever import BaseRetriever
    from llama_index.core.base.llms.types import ChatMessage, MessageRole
    from llama_index.core.bridge.pydantic import Field
    from llama_index.core.memory import Memory
    from llama_index.core.memory.memory import BaseMemoryBlock
    from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
    from llama_index.core.storage.chat_store.base_db import MessageStatus
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.llamaindex needs llama-index-core: "
        "pip install 'clickhouse-loom[llamaindex]'"
    ) from e

from loom.sync import SyncAgent


class LoomRetriever(BaseRetriever):
    """Document chunks from ``knowledge.search``, one ``NodeWithScore`` per chunk.

    The node's text is the chunk, its id ``<namespace>/<source_uri>#<chunk_idx>``,
    its score the similarity, and its metadata ``source_uri``, ``chunk_idx``,
    ``section`` (when the chunk has one), ``namespace`` and ``tags``.
    ``namespace_scope="this"`` searches the agent's namespace; ``all_mine`` adds
    the caller's other namespaces, up to the server's fan-out cap. ``retrieve``
    needs a ``loom.SyncAgent``; ``aretrieve`` takes a ``loom.Agent`` or a
    ``SyncAgent``.
    """

    def __init__(
        self,
        agent: Any,
        *,
        similarity_top_k: int = 4,
        tags: list[str] | None = None,
        uri_prefix: list[str] | None = None,
        namespace_scope: Literal["this", "all_mine"] = "this",
        search_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self._search_kwargs: dict[str, Any] = {**(search_kwargs or {}), "top_k": similarity_top_k}
        if tags:
            self._search_kwargs["tags"] = list(tags)
        if uri_prefix:
            self._search_kwargs["uri_prefix"] = list(uri_prefix)
        # Always sent: the server's own default is `all_mine`.
        self._search_kwargs["namespace_scope"] = namespace_scope

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        if not isinstance(self._agent, SyncAgent):
            raise TypeError(
                "LoomRetriever.retrieve needs a loom.SyncAgent; with a loom.Agent, call aretrieve"
            )
        hits = self._agent.knowledge.search(query_bundle.query_str, **self._search_kwargs)
        return _nodes(hits, self._agent.namespace)

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        if isinstance(self._agent, SyncAgent):
            # A SyncAgent blocks; its own loop thread serves the call.
            return await asyncio.to_thread(self._retrieve, query_bundle)
        hits = await self._agent.knowledge.search(query_bundle.query_str, **self._search_kwargs)
        return _nodes(hits, self._agent.namespace)


_BOOKKEEPING = ("chunk_idx", "namespace", "tags")


def _nodes(hits: list[Any], agent_namespace: str) -> list[NodeWithScore]:
    nodes = []
    for hit in hits:
        # A hit names its namespace only on a fan-out; otherwise it is the agent's.
        namespace = hit.namespace or agent_namespace
        node = TextNode(
            id_=f"{namespace}/{hit.source_uri}#{hit.chunk_idx}",
            text=hit.content,
            metadata={
                "source_uri": hit.source_uri,
                "chunk_idx": hit.chunk_idx,
                # Unstructured text has no section: no empty line in the model's text.
                **({"section": hit.section} if hit.section else {}),
                "namespace": namespace,
                "tags": list(hit.tags),
            },
            # Bookkeeping, not context: keep it out of the text a synthesizer sends the model.
            excluded_llm_metadata_keys=list(_BOOKKEEPING),
            excluded_embed_metadata_keys=list(_BOOKKEEPING),
        )
        nodes.append(NodeWithScore(node=node, score=hit.similarity))
    return nodes


class LoomMemoryBlock(BaseMemoryBlock[str]):
    """Long-term memory in Loom for LlamaIndex's ``Memory``.

    ``Memory`` hands the block only the messages that leave its short-term
    window, so call this module's ``aflush(memory)`` (``flush`` from sync
    code) when a session ends. ``aput`` sends the user and assistant turns to
    ``memory.set_from_messages`` under the message's session id; Loom extracts
    the facts and resolves duplicates and conflicts. With ``defer`` (the
    default) the server queues the extraction, so a flush costs one request
    rather than an LLM call. ``aget`` searches with the latest user message
    and returns the matching memories as one line each; with
    ``honour_abstain`` it leaves out memories below Loom's calibrated floor,
    and returns an empty string when Loom abstains.

    Pass a ``loom.SyncAgent`` when ``Memory`` is used through its sync methods
    (``get``, ``put``) or with this module's ``flush``, which may run a call on
    another event loop than the one a ``loom.Agent`` was opened on; an
    ``Agent`` serves ``aget`` / ``aput`` and ``aflush`` on its own loop.
    """

    name: str = Field(default="LoomMemories", description="The name of the memory block.")
    agent: Any = Field(description="The loom.Agent or loom.SyncAgent the block calls.")
    top_k: int = Field(default=5, description="Memories per read.")
    tags: list[str] | None = Field(
        default=None,
        description="Loom tags stamped on extracted memories; reads keep any of them.",
    )
    user_id: str | None = Field(
        default=None, description="The end user the memories belong to; unset reads every user's."
    )
    honour_abstain: bool = Field(
        default=True,
        description="Leave out memories below Loom's floor; nothing when Loom abstains.",
    )
    defer: bool = Field(
        default=True, description="Queue the extraction on the server; False extracts inline."
    )

    async def _run(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(self.agent, SyncAgent):
            # A SyncAgent blocks; its own loop thread serves the call.
            return await asyncio.to_thread(call, *args, **kwargs)
        return await call(*args, **kwargs)

    async def _aget(self, messages: list[ChatMessage] | None = None, **block_kwargs: Any) -> str:
        query = next(
            (
                m.content
                for m in reversed(messages or [])
                if m.role == MessageRole.USER and m.content
            ),
            None,
        )
        if not query:
            return ""
        # Read the namespace the block writes to, whatever the deployment's default;
        # with no agent_id the search reads every agent's memories there.
        kwargs: dict[str, Any] = {"top_k": self.top_k, "namespace_scope": "this"}
        if self.tags:
            kwargs["tags"] = list(self.tags)
        if self.user_id:
            kwargs["user_id"] = self.user_id
        result = await self._run(self.agent.memory.search, query, **kwargs)
        hits = list(result)
        if self.honour_abstain:
            if result.abstain:
                return ""
            # A response that does not abstain can still carry rows below the
            # floor; honouring abstention drops them.
            hits = [hit for hit in hits if not hit.below_threshold]
        # Derived rows may come back above the top_k slice: cut after filtering.
        hits = hits[: self.top_k]
        return "\n".join(f"- {hit.content_excerpt}" for hit in hits)

    async def _aput(self, messages: list[ChatMessage]) -> None:
        turns = [
            {"role": m.role.value, "content": m.content}
            for m in messages
            if m.content and m.role in (MessageRole.USER, MessageRole.ASSISTANT)
        ]
        if not turns:
            return
        session_id = next(
            (
                m.additional_kwargs["session_id"]
                for m in messages
                if "session_id" in m.additional_kwargs
            ),
            None,
        )
        await self._run(
            self.agent.memory.set_from_messages,
            turns,
            extra_tags=self.tags,
            user_id=self.user_id,
            session_id=session_id,
            defer=self.defer,
        )


async def aflush(memory: Memory) -> None:
    """Hand the messages still in ``memory``'s short-term window to its blocks.

    ``Memory`` hands its blocks only the messages that leave that window, so
    call this when a session ends. Like ``Memory``'s own hand-over, every block
    of ``memory`` gets the messages, and they are then archived, so the window
    starts empty. If a block fails, nothing is archived and the next flush hands
    the same messages over again: at least once, never lost.
    """
    active = await memory.aget_all(status=MessageStatus.ACTIVE)
    if not active:
        return
    # Every block finishes before a failure reaches the caller.
    results = await asyncio.gather(
        *(
            block.aput(active, from_short_term_memory=True, session_id=memory.session_id)
            for block in memory.memory_blocks
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    await memory.sql_store.archive_oldest_messages(memory.session_id, n=len(active))


def flush(memory: Memory) -> None:
    """``aflush`` for ``Memory``'s sync methods: every Loom block needs a ``SyncAgent``."""
    for block in memory.memory_blocks:
        if isinstance(block, LoomMemoryBlock) and not isinstance(block.agent, SyncAgent):
            raise TypeError("flush needs a loom.SyncAgent in every LoomMemoryBlock; call aflush")
    asyncio_run(aflush(memory))
