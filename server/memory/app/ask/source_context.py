"""Freeze document search scope before any ask work can use a provider."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

from server.knowledge.api.models_knowledge import KnowledgeSearchInput
from server.knowledge.app import knowledge_search, source_access
from server.knowledge.app.knowledge_search import SearchRequest
from server.knowledge.app.source_access import ReadPlan
from server.settings import settings


@dataclass(frozen=True)
class PreparedKnowledge:
    request: SearchRequest | None
    plan: ReadPlan

    @contextmanager
    def scope(self) -> Iterator[None]:
        from server.core.diagnostics import private_work
        from server.llm.cache import bypass

        with (
            private_work() if self.plan.marked else nullcontext(),
            bypass() if self.plan.marked else nullcontext(),
        ):
            yield


async def prepare_knowledge(
    input: Any, question: str, *, enabled: bool | None, corpus: bool = False
) -> PreparedKnowledge:
    width = int(settings.memory_ask_knowledge_top_k)
    if not corpus and (enabled is False or width <= 0):
        return PreparedKnowledge(None, ReadPlan(input.org))
    request = await knowledge_search.resolve_search(
        KnowledgeSearchInput(
            org=input.org,
            namespace=input.namespace,
            query=question,
            top_k=width if enabled is not False and width > 0 else 20,
            namespace_scope="this",
            tags=(
                []
                if bool(getattr(input, "tags_soft", False))
                else [str(tag) for tag in (getattr(input, "tags", None) or []) if tag]
            ),
            rerank="none",
        )
    )
    plan = await source_access.prepare(input.org, list(request.namespaces))
    return PreparedKnowledge(request, plan)


async def cancel_and_drain(task: asyncio.Task[Any]) -> None:
    """Join an owned child before another attempt or the request can finish."""
    import anyio

    from server.core.ch import set_query_tag

    set_query_tag("memory.ask.cleanup")
    task.cancel()
    parent = asyncio.current_task()
    cancellations = parent.cancelling() if parent is not None else 0
    interrupted = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if parent is not None and parent.cancelling() > cancellations:
                    cancellations = parent.cancelling()
                    interrupted = exc
            except BaseException:  # noqa: BLE001 — the caller owns failure; cleanup must join it
                break
    if not task.cancelled():
        task.exception()
    if interrupted is not None:
        raise interrupted


async def owned_attempt[T](work: Coroutine[Any, Any, T]) -> T:
    task = asyncio.create_task(work)
    try:
        return await asyncio.shield(task)
    finally:
        if not task.done():
            await cancel_and_drain(task)
