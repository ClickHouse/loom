"""loom as a Strands Agents ``MemoryStore``.

The Strands SDK's Memory Manager consumes any object with ``add()`` and
``search()`` (its ``MemoryStore`` protocol) and folds the results into the
model input before each call. This adapter gives a Strands agent loom's
memory — dedup on write, conflict detection, bitemporal validity,
calibrated abstention — through the SDK's own ``Agent``.

Usage::

    from loom import Agent
    from loom.integrations.strands import LoomMemoryStore
    from strands import Agent as StrandsAgent
    from strands.memory import MemoryManager

    async with Agent(org="acme", namespace="support", agent="assistant") as loom_agent:
        store = LoomMemoryStore(loom_agent, tags=["customer"])
        agent = StrandsAgent(memory_manager=MemoryManager(stores=[store], add_tool_config=True))
        agent("Which seat should I pick for my December trip?")

Identity (org, namespace, agent) comes from the loom ``Agent``; the
Strands side never sees a partition key. Tenant isolation is loom's
namespace authorisation, not a caller-supplied prefix.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent

try:  # the framework is optional — only the protocol shape is needed here
    from strands.memory import MemoryEntry
except Exception:  # noqa: BLE001 — ImportError or a strands version without it

    @dataclass
    class MemoryEntry:  # type: ignore[no-redef]
        """Structural twin of ``strands.memory.MemoryEntry`` for environments
        without the Strands SDK installed (tests, type checks)."""

        content: str
        store_name: str | None = None
        metadata: dict[str, Any] | None = field(default=None)


_META_KEYS_KEPT = ("kind", "source", "topic", "category")


def _content_key(prefix: str, content: str) -> str:
    """Content-addressed key: the same fact added twice is one row, and the
    server's supersession does the rest for near-duplicates."""
    digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:20]
    return f"{prefix}/{digest}"


class LoomMemoryStore:
    """Strands ``MemoryStore`` over a loom ``Agent``.

    ``add`` writes a ``memory.set`` with a content-addressed key and the
    metadata's short string values as tags. ``search`` runs ``memory.search``
    and returns entries newest-ranked first; when loom abstains (no hit
    above the calibrated threshold) the store returns nothing rather than a
    low-similarity row, so the Memory Manager injects no context at all, and
    a hit below the threshold in a response that does not abstain is left out.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        memory_type: str = "semantic",
        tags: list[str] | None = None,
        max_search_results: int = 3,
        honour_abstain: bool = True,
        key_prefix: str = "strands",
        name: str = "loom",
        description: str = "Long-term memories in loom, searched by meaning with abstention",
    ) -> None:
        self._agent = agent
        self._memory_type = memory_type
        self._tags = list(tags or [])
        self._honour_abstain = honour_abstain
        self._key_prefix = key_prefix.strip("/") or "strands"
        # MemoryStore protocol attributes read by the Memory Manager
        self.name = name
        self.description = description
        self.max_search_results = max_search_results
        self.writable = True
        self.extraction = None

    async def add(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        content = (content or "").strip()
        if not content:
            return
        tags = list(self._tags)
        for k in _META_KEYS_KEPT:
            v = (metadata or {}).get(k)
            if isinstance(v, str) and v and len(v) <= 64:
                tags.append(f"{k}:{v}")
        await self._agent.memory.set(
            _content_key(self._key_prefix, content),
            content,
            memory_type=self._memory_type,
            tags=tags or None,
        )

    async def search(
        self, query: str, options: Mapping[str, Any] | None = None
    ) -> list[MemoryEntry]:
        # Strands' SearchOptions TypedDict; the Memory Manager fills it per store
        top_k = int((options or {}).get("max_search_results") or self.max_search_results)
        result = await self._agent.memory.search(
            query,
            top_k=top_k,
            tags=self._tags or None,
            memory_type=self._memory_type,
            # Above top_k 15 an unpinned search serves 200-character excerpts.
            bodies="full",
            explain=False,
        )
        hits = list(result)
        if self._honour_abstain:
            if result.abstain:
                return []
            # A response that does not abstain can still carry rows below the
            # floor; honouring abstention drops them.
            hits = [hit for hit in hits if not hit.below_threshold]
        entries: list[MemoryEntry] = []
        # Derived rows may come back above the top_k slice: cut after filtering.
        for hit in hits[:top_k]:
            entries.append(
                MemoryEntry(
                    content=hit.content_excerpt,
                    metadata={
                        "memory_key": hit.memory_key,
                        "memory_type": hit.memory_type,
                        "similarity": round(float(hit.similarity), 4),
                        "source": hit.source,
                        **({"valid_at": hit.valid_at} if hit.valid_at else {}),
                    },
                )
            )
        return entries
