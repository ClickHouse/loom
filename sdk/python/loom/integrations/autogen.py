"""Loom as an AutoGen ``Memory``.

``LoomMemory`` implements ``autogen_core.memory.Memory``, the protocol an
AutoGen ``AssistantAgent`` takes as ``memory=[...]``: before each model call
the agent asks it to ``update_context``, and it adds the Loom memories that
match the latest user message as a system message::

    from autogen_agentchat.agents import AssistantAgent
    from loom import Agent
    from loom.integrations.autogen import LoomMemory

    async with Agent(org="acme", namespace="support") as loom_agent:
        memory = LoomMemory(loom_agent, user_id="alice")
        await memory.add(MemoryContent(content="Alice prefers aisle seats", mime_type="text/plain"))
        assistant = AssistantAgent("assistant", model_client=model_client, memory=[memory])

Identity and isolation come from the Loom agent. Install with
``pip install 'clickhouse-loom[autogen]'``.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

try:
    from autogen_core import CancellationToken
    from autogen_core.memory import (
        Memory,
        MemoryContent,
        MemoryMimeType,
        MemoryQueryResult,
        UpdateContextResult,
    )
    from autogen_core.model_context import ChatCompletionContext
    from autogen_core.models import SystemMessage, UserMessage
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.autogen needs autogen-core: pip install 'clickhouse-loom[autogen]'"
    ) from e

from loom.sync import SyncAgent

if TYPE_CHECKING:
    from loom.agent import Agent


class LoomMemory(Memory):
    """AutoGen ``Memory`` over a Loom ``Agent``.

    ``add`` writes one ``memory.set`` under a key derived from the content
    (``<key_prefix>/<first 20 hex of sha256>``), so adding the same text twice
    is one memory. ``query`` and ``update_context`` run ``memory.search`` of
    the namespace it writes to; with ``honour_abstain`` they leave out memories
    below Loom's floor and return nothing when Loom abstains. ``clear`` deletes
    the memories this adapter wrote under its key prefix, and nothing else.
    AutoGen awaits its memory, so the adapter takes a ``loom.Agent``.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        user_id: str | None = None,
        tags: list[str] | None = None,
        top_k: int = 5,
        honour_abstain: bool = True,
        key_prefix: str = "autogen",
    ) -> None:
        if isinstance(agent, SyncAgent):
            raise TypeError("LoomMemory needs a loom.Agent: AutoGen awaits its memory")
        self._agent = agent
        self._user_id = user_id
        self._tags = list(tags or [])
        self._top_k = top_k
        self._honour_abstain = honour_abstain
        self._key_prefix = key_prefix.strip("/") or "autogen"

    async def add(
        self, content: MemoryContent, cancellation_token: CancellationToken | None = None
    ) -> None:
        text = _text(content).strip()
        if not text:
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        await self._agent.memory.set(
            f"{self._key_prefix}/{digest}",
            text,
            tags=self._tags or None,
            user_id=self._user_id,
        )

    async def query(
        self,
        query: str | MemoryContent,
        cancellation_token: CancellationToken | None = None,
        **kwargs: Any,
    ) -> MemoryQueryResult:
        text = query if isinstance(query, str) else _text(query)
        if not text.strip():
            return MemoryQueryResult(results=[])
        top_k = min(int(kwargs.get("top_k", self._top_k)), 500)  # memory.search's bound
        result = await self._agent.memory.search(
            text,
            top_k=top_k,
            tags=self._tags or None,
            user_id=self._user_id,
            # Read the namespace this memory writes to, whatever the deployment's default.
            namespace_scope="this",
            # Above top_k 15 an unpinned search serves 200-character excerpts.
            bodies="full",
            explain=False,
        )
        hits = list(result)
        if self._honour_abstain:
            if result.abstain:
                return MemoryQueryResult(results=[])
            # A response that does not abstain can still carry rows below the floor.
            hits = [hit for hit in hits if not hit.below_threshold]
        return MemoryQueryResult(
            results=[
                MemoryContent(
                    content=hit.content_excerpt,
                    mime_type=MemoryMimeType.TEXT,
                    metadata={
                        "memory_key": hit.memory_key,
                        "memory_type": hit.memory_type,
                        "similarity": hit.similarity,
                        "source": hit.source,
                        **({"valid_at": hit.valid_at} if hit.valid_at else {}),
                    },
                )
                # Derived rows may come back above the top_k slice: cut after filtering.
                for hit in hits[:top_k]
            ]
        )

    async def update_context(self, model_context: ChatCompletionContext) -> UpdateContextResult:
        messages = await model_context.get_messages()
        query = next(
            (
                m.content
                for m in reversed(messages)
                if isinstance(m, UserMessage) and isinstance(m.content, str) and m.content
            ),
            "",
        )
        memories = await self.query(query) if query else MemoryQueryResult(results=[])
        if memories.results:
            # query returns text content only
            lines = "\n".join(f"{i}. {str(m.content)}" for i, m in enumerate(memories.results, 1))
            await model_context.add_message(
                SystemMessage(content=f"\nRelevant memory content from Loom:\n{lines}\n")
            )
        return UpdateContextResult(memories=memories)

    async def clear(self) -> None:
        keys: list[str] = []
        while True:  # every key first: deleting while paging would shift the pages
            page = await self._agent.memory.list(
                prefix=f"{self._key_prefix}/", offset=len(keys), user_id=self._user_id
            )
            keys.extend(entry["memory_key"] for entry in page["entries"])
            if not page.get("truncated"):
                break
        for key in keys:
            await self._agent.memory.delete(key, user_id=self._user_id)

    async def close(self) -> None:
        """The Loom agent belongs to the caller, who closes it."""


def _text(content: MemoryContent) -> str:
    if isinstance(content.content, str):
        return content.content
    if isinstance(content.content, dict):
        return json.dumps(content.content, ensure_ascii=False, sort_keys=True)
    raise TypeError(f"LoomMemory stores text and JSON, not {type(content.content).__name__}")
