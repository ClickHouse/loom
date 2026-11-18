"""A pack owns producer cancellation and freezes input before scope discovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.core import request_work
from server.core.diagnostics import codes_only
from server.knowledge.app import knowledge_search, source_access
from server.memory.api.models_context import ContextPackInput
from server.memory.app import context_pack


@pytest.fixture
def world(monkeypatch):
    token = request_work._current.set(None)
    request_work.begin()
    events = []
    state = SimpleNamespace(resolve_gate=None, resolve_entered=asyncio.Event(), marked=True)

    async def resolve(input):
        state.resolve_entered.set()
        events.append(("resolve", input.query))
        if state.resolve_gate is not None:
            await state.resolve_gate.wait()
        return knowledge_search.SearchRequest(input, (input.namespace, "other"), ())

    async def prepare(org, namespaces):
        events.append(("prepare", tuple(namespaces)))
        if state.marked:
            assert request_work.mark_mirrored()
        return source_access.ReadPlan(org, marked=state.marked)

    def producer(name):
        async def run(*args):
            events.append((name, codes_only()))
            return None

        return run

    monkeypatch.setattr(knowledge_search, "resolve_search", resolve)
    monkeypatch.setattr(source_access, "prepare", prepare)
    for name in (
        "_pack_working_memory",
        "_pack_profile",
        "_advisory_health",
        "_pack_namespace_guide",
        "_advisory_mentor",
        "_pack_memory_stack",
        "_pack_memories",
        "_pack_procedures",
        "_pack_knowledge",
        "_pack_conversations",
        "_pack_runs",
    ):
        monkeypatch.setattr(context_pack, name, producer(name))
    yield SimpleNamespace(events=events, state=state)
    request_work._current.reset(token)


def request():
    return ContextPackInput(
        org="acme",
        namespace="docs",
        agent="reader",
        query="original",
        situation="stack seed",
        include_knowledge=True,
    )


async def test_input_is_frozen_before_the_scope_await(world, monkeypatch):
    world.state.resolve_gate = asyncio.Event()
    original = request()
    observed = []

    async def direct(st, resolved, plan):
        observed.append(
            (
                st.input.query,
                st.input.situation,
                st.input.agent,
                resolved.input.query,
                resolved.namespaces,
                plan.marked,
            )
        )
        return None

    monkeypatch.setattr(context_pack, "_pack_knowledge", direct)
    task = asyncio.create_task(context_pack.handle_context_pack(original))
    try:
        await asyncio.wait_for(world.state.resolve_entered.wait(), timeout=5)
        original.query, original.situation, original.agent = "changed", "changed", "changed"
        world.state.resolve_gate.set()
        await asyncio.wait_for(task, timeout=5)
        assert observed == [
            ("original", "stack seed", "reader", "original", ("docs", "other"), True)
        ]
        assert world.events[:2] == [("resolve", "original"), ("prepare", ("docs", "other"))]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_every_sibling_starts_after_the_common_marked_preparation(world):
    output = await context_pack.handle_context_pack(request())
    assert world.events[:2] == [("resolve", "original"), ("prepare", ("docs", "other"))]
    providers = world.events[2:]
    assert len(providers) == 11 and all(private is True for name, private in providers)
    assert request_work.mirror_marked() and output.section_count == 0


async def test_independently_canceled_producer_drains_siblings_before_escape(world, monkeypatch):
    started, drained = asyncio.Event(), asyncio.Event()
    owned_waiters = []

    async def waiting(st):
        owned_waiters.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    async def canceled(st):
        await asyncio.wait_for(started.wait(), timeout=5)
        raise asyncio.CancelledError()

    monkeypatch.setattr(context_pack, "_pack_working_memory", waiting)
    monkeypatch.setattr(context_pack, "_pack_profile", canceled)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(context_pack.handle_context_pack(request()), timeout=5)
        assert drained.is_set()
    finally:
        for task in owned_waiters:
            task.cancel()
        await asyncio.gather(*owned_waiters, return_exceptions=True)


async def test_scope_cancellation_never_starts_preparation_or_producers(world, monkeypatch):
    async def canceled(input):
        raise asyncio.CancelledError()

    monkeypatch.setattr(knowledge_search, "resolve_search", canceled)
    with pytest.raises(asyncio.CancelledError):
        await context_pack.handle_context_pack(request())
    assert world.events == []
