"""An ask prepares source scope before route, client and generation-prefix work."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.core import request_work
from server.core.diagnostics import codes_only
from server.knowledge.app import knowledge_search, source_access
from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput
from server.memory.app import ask as ask_package
from server.memory.app.ask import handler
from server.memory.app.ask.result import MemoryAskResult
from server.settings import settings


@pytest.fixture
def world(monkeypatch):
    token = request_work._current.set(None)
    request_work.begin()
    state = SimpleNamespace(marked=True, routed=False, cancel_prepare=False)
    events = []

    def event(name):
        events.append((name, codes_only(), request_work.mirror_marked()))

    async def resolve(input):
        return knowledge_search.SearchRequest(input.model_copy(deep=True), ("docs",), ())

    async def prepare(org, namespaces):
        import asyncio

        assert org == "acme" and namespaces == ["docs"]
        events.append(("prepare", tuple(namespaces)))
        if state.cancel_prepare:
            raise asyncio.CancelledError()
        if state.marked:
            assert request_work.mark_mirrored()
        return source_access.ReadPlan(org, marked=state.marked)

    async def route(input, *_prepared, **_kwargs):
        event("route")
        if state.routed:
            return MemoryAskOutput(
                answer="route answer", source="synthesized", route_reason="corpus"
            )
        return None

    async def clients(*_args):
        event("clients")
        return None, None, None, None, False, 0.0

    def precedent(*_args):
        event("precedent")
        return "", ""

    async def scratchpad(*_args):
        event("scratchpad")
        return "ordinary run notes"

    async def pipeline(*_args, **_kwargs):
        event("pipeline")
        return MemoryAskResult(source="retrieval_only")

    async def pending(*_args):
        event("pending")
        return 7

    monkeypatch.setattr(settings, "memory_ask_knowledge_top_k", 2)
    monkeypatch.setattr(knowledge_search, "resolve_search", resolve)
    monkeypatch.setattr(source_access, "prepare", prepare)
    monkeypatch.setattr(handler, "refuse_synthesis", lambda *_args: None)
    monkeypatch.setattr(handler, "_maybe_route_to_synthesize", route)
    monkeypatch.setattr(handler, "_ask_clients", clients)
    monkeypatch.setattr(handler, "_ask_precedent", precedent)
    monkeypatch.setattr("server.memory.app.working_memory.render_run_blocks", scratchpad)
    monkeypatch.setattr(ask_package, "ask", pipeline)
    monkeypatch.setattr(handler, "_pending_extraction", pending)
    monkeypatch.setattr(handler, "_schedule_cited_used", lambda *_args: None)
    monkeypatch.setattr(handler, "_record_contradiction", lambda *_args: None)
    monkeypatch.setattr(handler, "_schedule_deferred_consistency", lambda *_args: None)
    yield SimpleNamespace(state=state, events=events)
    request_work._current.reset(token)


def request(**changes):
    return MemoryAskInput(
        org="acme",
        namespace="docs",
        agent="reader",
        query="question",
        run_id="run-one",
        knowledge=True,
        allow_synthesis=False,
    ).model_copy(update=changes)


async def test_marked_ask_prepares_before_clients_prefix_and_all_pipeline_work(world):
    output = await handler.handle_memory_ask(request())
    assert world.events[0] == ("prepare", ("docs",))
    names = [row[0] for row in world.events]
    assert names.count("prepare") == 1
    for name in ("clients", "precedent", "scratchpad", "pipeline", "pending"):
        assert (name, True, True) in world.events
    assert names.index("pending") < names.index("pipeline")
    assert output.source == "retrieval_only" and output.pending_extraction == 7


async def test_corpus_route_is_prepared_even_when_normal_passages_are_disabled(world):
    world.state.routed = True
    output = await handler.handle_memory_ask(
        request(allow_synthesis=True, knowledge=False, query="summarize all documents")
    )
    assert world.events == [("prepare", ("docs",)), ("route", True, True)]
    assert output.answer == "route answer" and output.route_reason == "corpus"


async def test_ordinary_plan_still_prepares_but_keeps_ordinary_tail_timing(world):
    world.state.marked = False
    output = await handler.handle_memory_ask(request())
    assert world.events[0] == ("prepare", ("docs",))
    names = [row[0] for row in world.events]
    assert names.index("pending") > names.index("pipeline")
    assert all(row[1:] == (False, False) for row in world.events if row[0] != "prepare")
    assert not request_work.mirror_marked() and output.pending_extraction == 7


async def test_no_possible_document_read_does_not_discover_or_mark(world):
    output = await handler.handle_memory_ask(request(knowledge=False))
    assert all(row[0] != "prepare" for row in world.events)
    assert all(row[1:] == (False, False) for row in world.events)
    assert output.source == "retrieval_only" and not request_work.mirror_marked()


async def test_source_preparation_cancellation_starts_no_other_work(world):
    import asyncio

    world.state.cancel_prepare = True
    with pytest.raises(asyncio.CancelledError):
        await handler.handle_memory_ask(request())
    assert world.events == [("prepare", ("docs",))]
