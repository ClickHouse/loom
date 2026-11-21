"""The ask's knowledge legs carry its hard tag filter.

A scoped `memory.ask` (tags=["workflow:x"]) must not read documents from every
workflow in its knowledge searches; every row over the document threshold is
also a document, so the pool would leak. Both legs forward the request's tags
unless `tags_soft` made them a boost.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.app.ask.context_blocks import knowledge_passages
from server.memory.app.ask.handler import _hard_tags
from tests.unit.ask_knowledge_fake import checked_reply, route_scope
from tests.unit.ask_knowledge_fake import ordinary_knowledge_scope as ordinary_knowledge_scope

pytestmark = pytest.mark.usefixtures("ordinary_knowledge_scope")


def test_hard_tags_follow_tags_soft() -> None:
    def ask(**kw: object) -> MemoryAskInput:
        return MemoryAskInput(org="o", namespace="n", query="q", **kw)

    assert _hard_tags(ask(tags=["workflow:x", ""], tags_soft=False)) == ["workflow:x"]
    assert _hard_tags(ask(tags=["workflow:x"], tags_soft=True)) == []
    assert _hard_tags(ask()) == []


@pytest.mark.asyncio
async def test_knowledge_passages_forward_the_asks_tags(monkeypatch) -> None:
    import server.knowledge.app.knowledge_search as kh

    seen: list[list[str]] = []
    reranks: list[str] = []

    async def ok(request, _plan):
        inp = request.input
        seen.append(list(inp.tags))
        reranks.append(inp.rerank)
        return checked_reply(SimpleNamespace(results=[]))

    monkeypatch.setattr(kh, "run_checked_search", ok)
    hard = SimpleNamespace(org="o", namespace="n", agent="*", query="q", tags=["workflow:x"])
    soft = SimpleNamespace(
        org="o", namespace="n", agent="*", query="q", tags=["workflow:x"], tags_soft=True
    )
    await knowledge_passages(hard, "q", enabled=True)
    await knowledge_passages(soft, "q", enabled=True)
    assert seen == [["workflow:x"], []]
    assert reranks == ["none", "none"]  # an ask's knowledge read makes no rerank call


@pytest.mark.asyncio
async def test_synthesize_route_forwards_the_asks_tags(monkeypatch) -> None:
    import server.knowledge.app.knowledge_search as kh
    from server.memory.app.ask import handler as ma
    from server.memory.domain import ask_routing

    monkeypatch.setattr(ask_routing, "is_corpus_task", lambda q: True)
    seen: list[list[str]] = []

    async def ok(request, _plan):
        inp = request.input
        seen.append(list(inp.tags))
        assert inp.rerank == "none"
        return checked_reply(SimpleNamespace(results=[]))

    monkeypatch.setattr(kh, "run_checked_search", ok)
    inp = MemoryAskInput(
        org="o", namespace="n", agent="a", query="summarise the reports", tags=["workflow:x"]
    )
    assert await ma._maybe_route_to_synthesize(inp, await route_scope(inp)) is None
    assert seen == [["workflow:x"]]
