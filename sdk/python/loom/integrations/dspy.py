"""Loom as DSPy retrievers.

``LoomKnowledgeRetriever`` searches the documents in a Loom namespace
(``knowledge.search``) and ``LoomMemoryRetriever`` its memories
(``memory.search``). Each is a plain callable, the shape of DSPy's own
``dspy.retrievers.Embeddings``: a program calls it in ``forward`` and gets a
``dspy.Prediction`` whose ``passages`` are the texts, with ``sources``, ``scores``
and ``metadata`` alongside so the program can cite what it used. ``as_tool()``
hands the same search to ``dspy.ReAct``.

Usage::

    import dspy
    from loom import SyncAgent
    from loom.integrations.dspy import LoomKnowledgeRetriever

    with SyncAgent(org="acme", namespace="support") as agent:
        search = LoomKnowledgeRetriever(agent, k=3)
        found = search("refund policy for annual plans")
        found.passages, found.sources

        react = dspy.ReAct("question -> answer", tools=[search.as_tool()])

Call it with a ``SyncAgent``; ``await retriever.acall(query)`` takes an ``Agent``
or a ``SyncAgent``. Identity and isolation come from the agent: the retriever
reads only what its token may read. Install with
``pip install 'clickhouse-loom[dspy]'``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any, ClassVar, Literal

try:
    import dspy
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.dspy needs dspy: pip install 'clickhouse-loom[dspy]'"
    ) from e

from loom.integrations._session_protection import _CallbackProtection
from loom.session_protection import automatic_roots
from loom.sync import SyncAgent


def _check_k(k: object) -> int:
    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive int")
    return k


class _LoomRetriever(ABC):
    client_name: ClassVar[str]  # the agent's client the search runs on
    tool_name: ClassVar[str]
    tool_desc: ClassVar[str]

    def __init__(self, agent: Any, *, k: int, search_tags: list[str] | None) -> None:
        self.agent = agent
        self.k = _check_k(k)
        self.search_tags = list(search_tags) if search_tags else None
        self._protection = _CallbackProtection(self._bound())

    def _bound(self) -> Any:
        return getattr(getattr(self.agent, "transport", None), "session_protection", None)

    def _callback_scope(self) -> AbstractContextManager[None]:
        if automatic_roots():
            return self._protection.automatic(self._bound())
        return self._protection.observing(self._bound())

    @abstractmethod
    def _call_kwargs(self, k: int) -> dict[str, Any]: ...

    @abstractmethod
    def _rows(self, result: Any, k: int) -> list[tuple[str, str, float, dict[str, Any]]]:
        """(passage, source, score, metadata) per hit, best first."""

    def _prediction(self, result: Any, k: int) -> dspy.Prediction:
        rows = self._rows(result, k)
        return dspy.Prediction(
            passages=[row[0] for row in rows],
            sources=[row[1] for row in rows],
            scores=[row[2] for row in rows],
            metadata=[row[3] for row in rows],
        )

    def _sync_search(self, query: str, k: int) -> dspy.Prediction:
        client = getattr(self.agent, self.client_name)
        return self._prediction(client.search(query, **self._call_kwargs(k)), k)

    def __call__(self, query: str, k: int | None = None) -> dspy.Prediction:
        """Search Loom; ``k`` overrides the retriever's own for this call."""
        return self.forward(query, k)

    def forward(self, query: str, k: int | None = None) -> dspy.Prediction:
        if not isinstance(self.agent, SyncAgent):
            raise TypeError(
                f"{type(self).__name__}() needs a loom.SyncAgent; with a loom.Agent, await acall()"
            )
        n = self.k if k is None else _check_k(k)
        with self._callback_scope():
            return self._sync_search(query, n)

    async def acall(self, query: str, k: int | None = None) -> dspy.Prediction:
        n = self.k if k is None else _check_k(k)
        with self._callback_scope():
            if isinstance(self.agent, SyncAgent):
                # A SyncAgent blocks; its own loop thread serves the call.
                return await asyncio.to_thread(self._sync_search, query, n)
            client = getattr(self.agent, self.client_name)
            return self._prediction(await client.search(query, **self._call_kwargs(n)), n)

    def as_tool(self, name: str | None = None) -> dspy.Tool:
        """The search as a ``dspy.Tool`` for ``dspy.ReAct``: it returns one
        ``[source] passage`` line per hit, so the model sees where each came from.
        Sync with a ``SyncAgent``, async (``acall``) with an ``Agent``."""

        def lines(found: dspy.Prediction) -> list[str]:
            return [f"[{s}] {p}" for p, s in zip(found.passages, found.sources, strict=True)]

        func: Callable[[str], list[str]] | Callable[[str], Awaitable[list[str]]]
        if isinstance(self.agent, SyncAgent):

            def search(query: str) -> list[str]:
                return lines(self(query))

            func = search
        else:

            async def asearch(query: str) -> list[str]:
                return lines(await self.acall(query))

            func = asearch
        return dspy.Tool(func, name=name or self.tool_name, desc=self.tool_desc)


class LoomKnowledgeRetriever(_LoomRetriever):
    """Document chunks from ``knowledge.search``, one passage per chunk.

    A passage is the chunk text; its source is ``<namespace>/<source_uri>#<chunk_idx>``,
    its score the similarity, and its metadata carries ``source_uri``, ``chunk_idx``,
    ``similarity``, ``section``, ``namespace`` and ``tags``.
    """

    client_name = "knowledge"
    tool_name = "loom_search_documents"
    tool_desc = (
        "Search the team's documents in Loom. Returns matching passages, "
        "each prefixed with its [source] to cite."
    )

    def __init__(
        self,
        agent: Any,
        *,
        k: int = 3,
        search_tags: list[str] | None = None,
        uri_prefix: list[str] | None = None,
        namespace_scope: Literal["this", "all_mine"] = "this",
        search_kwargs: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(agent, k=k, search_tags=search_tags)
        if namespace_scope not in ("this", "all_mine"):
            raise ValueError("namespace_scope must be 'this' or 'all_mine'")
        self.uri_prefix = list(uri_prefix) if uri_prefix else None
        self.namespace_scope = namespace_scope
        self.search_kwargs = dict(search_kwargs or {})

    def _call_kwargs(self, k: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = {**self.search_kwargs, "top_k": k}
        if self.search_tags:
            kwargs["tags"] = list(self.search_tags)
        if self.uri_prefix:
            kwargs["uri_prefix"] = list(self.uri_prefix)
        # Always sent: the server's own default is `all_mine`.
        kwargs["namespace_scope"] = self.namespace_scope
        return kwargs

    def _rows(self, result: Any, k: int) -> list[tuple[str, str, float, dict[str, Any]]]:
        rows = []
        for hit in list(result)[:k]:
            # A hit names its namespace only on a fan-out; otherwise it is the agent's.
            namespace = hit.namespace or self.agent.namespace
            score = round(float(hit.similarity), 4)
            metadata = {
                "source_uri": hit.source_uri,
                "chunk_idx": hit.chunk_idx,
                "similarity": score,
                "section": hit.section,
                "namespace": namespace,
                "tags": list(hit.tags),
            }
            source = f"{namespace}/{hit.source_uri}#{hit.chunk_idx}"
            rows.append((hit.content, source, score, metadata))
        return rows


class LoomMemoryRetriever(_LoomRetriever):
    """Memories from ``memory.search``, one passage per memory.

    A passage is the memory's text; its source is ``<namespace>/<memory_key>``, its
    score the similarity, and its metadata carries ``namespace``, ``memory_key``,
    ``memory_type``, ``similarity``, ``source`` and, when set, ``valid_at``. When Loom
    abstains (no memory above its calibrated floor) the prediction has no passages, and
    a memory below the floor in a response that does not abstain is left out, unless
    ``honour_abstain`` is false.
    """

    client_name = "memory"
    tool_name = "loom_search_memories"
    tool_desc = (
        "Search what Loom remembers (facts, preferences, past decisions). Returns "
        "matching memories, each prefixed with its [source] to cite."
    )

    def __init__(
        self,
        agent: Any,
        *,
        k: int = 3,
        search_tags: list[str] | None = None,
        memory_type: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        honour_abstain: bool = True,
    ) -> None:
        super().__init__(agent, k=k, search_tags=search_tags)
        self.memory_type = memory_type
        self.user_id = user_id
        self.session_id = session_id
        self.run_id = run_id
        self.agent_id = agent_id
        self.honour_abstain = honour_abstain

    def _call_kwargs(self, k: int) -> dict[str, Any]:
        # Unpinned, a large top_k serves excerpts (above 15 by default)
        # and drops each hit's explanation.
        kwargs: dict[str, Any] = {"top_k": k, "bodies": "full", "explain": False}
        if self.search_tags:
            kwargs["tags"] = list(self.search_tags)
        for field in ("memory_type", "user_id", "session_id", "run_id", "agent_id"):
            if getattr(self, field):
                kwargs[field] = getattr(self, field)
        return kwargs

    def _rows(self, result: Any, k: int) -> list[tuple[str, str, float, dict[str, Any]]]:
        hits = list(result)
        if self.honour_abstain:
            if result.abstain:
                return []
            # A response that does not abstain can still carry rows below the
            # floor; honouring abstention drops them.
            hits = [hit for hit in hits if not hit.below_threshold]
        rows = []
        # Derived rows may come back above the top_k slice: cut after filtering.
        for hit in hits[:k]:
            # Named on a fan-out (the server's memory_search_default_namespace_scope).
            namespace = hit.namespace or self.agent.namespace
            score = round(float(hit.similarity), 4)
            metadata: dict[str, Any] = {
                "namespace": namespace,
                "memory_key": hit.memory_key,
                "memory_type": hit.memory_type,
                "similarity": score,
                "source": hit.source,
            }
            if hit.valid_at:
                metadata["valid_at"] = hit.valid_at
            rows.append((hit.content_excerpt, f"{namespace}/{hit.memory_key}", score, metadata))
        return rows
