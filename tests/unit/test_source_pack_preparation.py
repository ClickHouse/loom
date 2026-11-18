"""A pack prepares every possible knowledge scope before sibling providers."""

from __future__ import annotations

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
    state = {"resolution_error": False, "marked": True}

    async def resolve(input):
        events.append(("resolve", input.model_copy(deep=True), codes_only()))
        if state["resolution_error"]:
            raise ValueError("private resolution failure")
        return knowledge_search.SearchRequest(input.model_copy(deep=True), ("docs", "other"), ())

    async def prepare(org, namespaces):
        assert org == "acme"
        events.append(("prepare", tuple(namespaces), codes_only()))
        if state["marked"]:
            assert request_work.mark_mirrored()
        return source_access.ReadPlan(org, marked=state["marked"])

    async def gather(_state, *_prepared):
        events.append(("providers", None, codes_only()))
        return None, None

    monkeypatch.setattr(knowledge_search, "resolve_search", resolve)
    monkeypatch.setattr(source_access, "prepare", prepare)
    monkeypatch.setattr(context_pack, "_gather_sections", gather)
    yield SimpleNamespace(state=state, events=events)
    request_work._current.reset(token)


def request(sections, **changes):
    return ContextPackInput(
        org="acme",
        namespace="docs",
        agent="reader",
        include_knowledge=True,
        only_sections=sections,
        **changes,
    )


@pytest.mark.parametrize(
    "sections,query,situation,expected,resolve",
    [
        (["knowledge"], "question", "different stack seed", ("docs", "other"), True),
        (["memory_stack"], "", "different stack seed", ("docs",), False),
        (["memory_stack"], "", "", ("docs",), False),
        (
            ["knowledge", "memory_stack"],
            "question",
            "different stack seed",
            ("docs", "other"),
            True,
        ),
    ],
)
async def test_parent_prepares_fixed_union_before_any_sibling_provider(
    world, sections, query, situation, expected, resolve
):
    output = await context_pack.handle_context_pack(
        request(sections, query=query, situation=situation)
    )
    assert [event[0] for event in world.events] == (
        ["resolve", "prepare", "providers"] if resolve else ["prepare", "providers"]
    )
    prepared = [event for event in world.events if event[0] == "prepare"]
    assert len(prepared) == 1 and prepared[0][1] == expected
    assert world.events[-1] == ("providers", None, True)
    assert request_work.mirror_marked()
    assert output.section_count == 0 and not output.rendered and not output.truncated
    if resolve:
        source = world.events[0][1]
        assert source.query == query and source.namespace_scope == "all_mine"
        assert source.rerank == "none" and source.namespaces == []


@pytest.mark.parametrize(
    "changes",
    [
        {"include_knowledge": False},
        {"only_sections": ["profile"]},
        {"only_sections": ["knowledge"], "query": ""},
    ],
)
async def test_no_possible_knowledge_read_keeps_the_parent_unmarked(world, changes):
    input = request(["knowledge", "memory_stack"], query="question").model_copy(update=changes)
    output = await context_pack.handle_context_pack(input)
    assert world.events == [("providers", None, False)]
    assert not request_work.mirror_marked()
    assert output.section_count == 0 and not output.rendered


async def test_direct_scope_failure_keeps_prepared_stack_and_unrelated_sections(world, caplog):
    world.state["resolution_error"] = True
    output = await context_pack.handle_context_pack(
        request(["knowledge", "memory_stack"], query="question")
    )
    assert [event[0] for event in world.events] == ["resolve", "prepare", "providers"]
    assert world.events[1][1] == ("docs",)
    assert world.events[-1] == ("providers", None, True)
    assert output.section_count == 0 and "private resolution failure" not in caplog.text


async def test_ordinary_plan_keeps_provider_work_and_empty_output_unmarked(world):
    world.state["marked"] = False
    output = await context_pack.handle_context_pack(request(["knowledge"], query="question"))
    assert [event[0] for event in world.events] == ["resolve", "prepare", "providers"]
    assert world.events[-1] == ("providers", None, False)
    assert not request_work.mirror_marked()
    assert output.section_count == 0 and not output.rendered
