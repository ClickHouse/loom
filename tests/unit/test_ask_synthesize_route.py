"""A summary-shaped memory.ask goes to the whole document."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.domain import ask_routing


@pytest.mark.parametrize(
    "q",
    [
        "Summarize the incident report",
        "give me a TL;DR of this thread",
        "What is this document about?",
        "What are the main themes across the whole book",
        "key takeaways from the design review",
    ],
)
def test_corpus_tasks_detected(q):
    assert ask_routing.is_corpus_task(q)


@pytest.mark.parametrize(
    "q",
    [
        "When did Alice move to Paris?",
        "which port does the server listen on",
        "who reviewed PR 412",
        "",
    ],
)
def test_needle_questions_not_routed(q):
    assert not ask_routing.is_corpus_task(q)


def _hits(*uris):
    return [SimpleNamespace(source_uri=u) for u in uris]


def test_pick_documents_takes_the_concentrated_ones_in_order():
    hits = _hits("b", "a", "a", "b", "a", "c")
    assert ask_routing.pick_documents(hits, min_share=0.5) == ["a"]
    assert ask_routing.pick_documents(hits, min_share=0.9) == ["a", "b"]  # c has one hit


def test_pick_documents_empty_when_spread_thin():
    assert ask_routing.pick_documents(_hits("a", "b", "c", "d")) == []
    assert ask_routing.pick_documents([]) == []
    assert ask_routing.pick_documents(_hits("", "")) == []


def test_pick_documents_caps_at_max_docs():
    hits = _hits("a", "a", "b", "b", "c", "c", "d", "d")
    assert ask_routing.pick_documents(hits, max_docs=2, min_share=1.0) == ["a", "b"]


@pytest.mark.asyncio
async def test_handler_routes_only_when_flag_on_and_documents_concentrate(monkeypatch):
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeOutput
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.app.ask import handler as h

    calls: list[str] = []

    async def fake_search(inp):
        calls.append("search")
        return SimpleNamespace(results=_hits("doc://r", "doc://r", "doc://x"))

    async def fake_synth(inp):
        calls.append("synth:" + ",".join(inp.source_uris))
        return KnowledgeSynthesizeOutput(
            answer="The report says X.",
            windows=2,
            chunks_read=9,
            documents=1,
            reduce_rounds=1,
            model="m",
        )

    import server.knowledge.app.knowledge_search as ks
    import server.knowledge.app.synthesize as ksyn

    monkeypatch.setattr(ks, "handle_knowledge_search", fake_search)
    monkeypatch.setattr(ksyn, "handle_knowledge_synthesize", fake_synth)
    inp = MemoryAskInput(org="o", namespace="n", query="summarize the report")

    out = await h._maybe_route_to_synthesize(inp)
    assert out is not None
    assert out.answer == "The report says X."
    assert out.route_reason == "knowledge_synthesize:1_docs"
    assert out.synth_model == "m"
    assert calls == ["search", "synth:doc://r"]

    # Memories only, or an as-of replay: the route never runs.
    calls.clear()
    no_synth = MemoryAskInput(
        org="o", namespace="n", query="summarize the report", allow_synthesis=False
    )
    assert await h._maybe_route_to_synthesize(no_synth) is None
    replay = MemoryAskInput(
        org="o", namespace="n", query="summarize the report", as_of="2026-01-01T00:00:00Z"
    )
    assert await h._maybe_route_to_synthesize(replay) is None
    assert calls == []

    # A needle question never reaches the knowledge plane.
    calls.clear()
    needle = MemoryAskInput(org="o", namespace="n", query="who wrote the report")
    assert await h._maybe_route_to_synthesize(needle) is None
    assert calls == []


@pytest.mark.asyncio
async def test_handler_falls_through_when_synthesis_abstains(monkeypatch):
    import server.knowledge.app.knowledge_search as ks
    import server.knowledge.app.synthesize as ksyn
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeOutput
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.app.ask import handler as h

    async def fake_search(inp):
        return SimpleNamespace(results=_hits("doc://r", "doc://r"))

    async def fake_synth(inp):
        return KnowledgeSynthesizeOutput(
            answer="",
            windows=0,
            chunks_read=0,
            documents=0,
            reduce_rounds=0,
            abstain=True,
            abstain_reason="no_content",
        )

    monkeypatch.setattr(ks, "handle_knowledge_search", fake_search)
    monkeypatch.setattr(ksyn, "handle_knowledge_synthesize", fake_synth)
    inp = MemoryAskInput(org="o", namespace="n", query="overview of the report")
    assert await h._maybe_route_to_synthesize(inp) is None
