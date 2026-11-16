"""Loom as a CrewAI crew's memory.

``LoomCrewMemory`` is a ``crewai.memory.Memory``, so a crew takes it as
``memory=``; set it on the crew, not on an agent, which CrewAI rebuilds with
its own memory when it copies the crew. What a crew run asks of its memory
goes to Loom:

- after a task, the crew asks memory to extract facts from the output and
  save them: ``extract_memories`` hands the output through unchanged and
  ``remember_many`` sends it to ``memory.set_from_messages``, where Loom
  extracts the facts and resolves duplicates and conflicts;
- before a task, ``recall`` runs ``memory.search``;
- the memory tools' explicit save of one item (``remember``) writes one
  ``memory.set``; a save of several goes through ``remember_many``.

It needs no CrewAI LLM, embedder or local store: Loom embeds and extracts on
the server::

    from crewai import Crew
    from loom import SyncAgent
    from loom.integrations.crewai import LoomCrewMemory

    with SyncAgent(org="acme", namespace="research") as agent:
        crew = Crew(agents=[...], tasks=[...], memory=LoomCrewMemory(agent=agent))
        crew.kickoff()

Every agent of the crew shares the namespace's memory; identity and isolation
come from the Loom agent. Install with ``pip install 'clickhouse-loom[crewai]'``.
"""

from __future__ import annotations

import hashlib
from typing import Any

try:
    from crewai.memory.types import MemoryMatch, MemoryRecord
    from crewai.memory.unified_memory import Memory
    from pydantic import Field
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.crewai needs crewai: pip install 'clickhouse-loom[crewai]'"
    ) from e

from loom.sync import SyncAgent

_NO_SCOPES = (
    "LoomCrewMemory has no scope tree, slices or record listing: Loom scopes memory by "
    "namespace and user_id; read it with recall"
)


class LoomCrewMemory(Memory):
    """CrewAI ``Memory`` over a ``loom.SyncAgent``.

    ``recall`` returns the matches above Loom's calibrated floor (with
    ``honour_abstain``, rows below it are left out), or nothing when Loom
    abstains, with ``score`` the similarity. ``tags`` and the ``categories`` of
    a save become Loom tags. A recall keeps memories carrying any of ``tags``
    and, when ``categories`` are given, any of them too. The scope tree,
    slices, private records and LLM analysis of CrewAI's own memory are not
    offered: those methods raise, and so do ``reset`` and ``forget``, because
    Loom deletes a memory only when its key is named (``memory.delete``) or
    through erasure. Do not checkpoint a crew that holds this memory: CrewAI
    saves it as its own ``Memory`` and restores that, with a local store.
    """

    agent: SyncAgent = Field(
        description="The loom.SyncAgent the memory calls; crews call memory synchronously."
    )
    user_id: str | None = Field(default=None, description="The end user the memories belong to.")
    tags: list[str] = Field(
        default_factory=list,
        description="Loom tags stamped on every save; recalls keep memories carrying any of them.",
    )
    honour_abstain: bool = Field(
        default=True,
        description="Leave out memories below Loom's floor; nothing when Loom abstains.",
    )
    defer: bool = Field(
        default=True, description="Queue the extraction on the server instead of waiting for it."
    )
    key_prefix: str = Field(default="crewai", description="Key prefix of explicit saves.")

    def model_post_init(self, __context: Any) -> None:
        """No CrewAI LLM, embedder or storage: Loom does that work."""

    def extract_memories(self, content: str) -> list[str]:
        """Loom extracts the facts when ``remember_many`` sends the text."""
        return [content] if content.strip() else []

    def remember(
        self,
        content: str,
        scope: str | None = None,
        categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        importance: float | None = None,
        source: str | None = None,
        private: bool = False,
        agent_role: str | None = None,
        root_scope: str | None = None,
    ) -> MemoryRecord | None:
        if self.read_only:
            return None
        _refuse_private(private)
        text = content.strip()
        if not text:
            raise ValueError("nothing to remember: the content is empty")
        key = f"{self.key_prefix}/{hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]}"
        tags = self._tags(categories)
        self.agent.memory.set(key, text, tags=tags or None, user_id=self.user_id)
        return MemoryRecord(id=key, content=text, scope="/", categories=tags)

    def remember_many(
        self,
        contents: list[str],
        scope: str | None = None,
        categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        importance: float | None = None,
        source: str | None = None,
        private: bool = False,
        agent_role: str | None = None,
        root_scope: str | None = None,
    ) -> list[MemoryRecord]:
        """Send the texts for extraction; the memories Loom keeps are its own rows."""
        if self.read_only:
            return []
        _refuse_private(private)
        turns = [{"role": "assistant", "content": c} for c in contents if c.strip()]
        if turns:
            self.agent.memory.set_from_messages(
                turns,
                extra_tags=self._tags(categories) or None,
                user_id=self.user_id,
                defer=self.defer,
            )
        return []

    def recall(
        self,
        query: str,
        scope: str | None = None,
        categories: list[str] | None = None,
        limit: int = 10,
        depth: str = "deep",
        source: str | None = None,
        include_private: bool = False,
    ) -> list[MemoryMatch]:
        if not query.strip():
            return []
        # A search keeps rows carrying any listed tag, so the memory's own tags go
        # to the server and the categories narrow its rows here, from a pool of
        # five times the limit: at least 50, at most memory.search's bound of 500.
        wanted = set(categories) if self.tags and categories else set()
        result = self.agent.memory.search(
            query,
            top_k=min(max(limit * 5, 50) if wanted else limit, 500),
            # Unpinned, a large top_k serves excerpts (above 15 by default)
            # and drops each hit's explanation.
            bodies="full",
            explain=False,
            # CrewAI's read-only memory does not refresh access times on recall.
            auto_touch=not self.read_only,
            tags=list(self.tags or categories or []) or None,
            include_tags=bool(wanted),
            user_id=self.user_id,
            # Read the namespace the crew writes to, whatever the deployment's default.
            namespace_scope="this",
        )
        hits = list(result)
        if wanted:
            hits = [hit for hit in hits if wanted & set(hit.tags)]
        if self.honour_abstain:
            if result.abstain:
                return []
            # A response that does not abstain can still carry rows below the floor.
            hits = [hit for hit in hits if not hit.below_threshold]
        # Derived rows may come back above the top_k slice: cut after filtering.
        return [
            MemoryMatch(
                record=MemoryRecord(
                    id=hit.memory_key,
                    content=hit.content_excerpt,
                    metadata={"memory_key": hit.memory_key, "memory_type": hit.memory_type},
                ),
                score=hit.similarity,
                match_reasons=["semantic"],
            )
            for hit in hits[:limit]
        ]

    def drain_writes(self) -> None:
        """Every save is a request that has returned; nothing is pending."""

    def close(self) -> None:
        """The Loom agent belongs to the caller, who closes it."""

    def reset(self, scope: str | None = None) -> None:
        raise NotImplementedError(
            "LoomCrewMemory does not reset: delete a memory by key with memory.delete, "
            "or a user's memories through Loom's erasure"
        )

    def forget(self, *args: Any, **kwargs: Any) -> int:
        raise NotImplementedError(
            "LoomCrewMemory does not forget by scope or age: "
            "delete a memory by key with memory.delete"
        )

    def update(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "LoomCrewMemory does not edit a record: save the new text with remember"
        )

    def scope(self, path: str) -> Any:
        raise NotImplementedError(_NO_SCOPES)

    def slice(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(_NO_SCOPES)

    def list_scopes(self, path: str | None = None) -> list[str]:
        raise NotImplementedError(_NO_SCOPES)

    def list_records(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(_NO_SCOPES)

    def info(self, path: str | None = None) -> Any:
        raise NotImplementedError(_NO_SCOPES)

    def tree(self, path: str | None = None, max_depth: int = 3) -> str:
        raise NotImplementedError(_NO_SCOPES)

    def list_categories(self, path: str | None = None) -> dict[str, int]:
        raise NotImplementedError(_NO_SCOPES)

    def reset_all(self) -> None:
        self.reset()

    def _tags(self, categories: list[str] | None) -> list[str]:
        return [*self.tags, *(categories or [])]


def _refuse_private(private: bool) -> None:
    if private:
        raise NotImplementedError(
            "LoomCrewMemory has no private records: a memory is visible to its namespace, "
            "or to one end user with user_id"
        )
