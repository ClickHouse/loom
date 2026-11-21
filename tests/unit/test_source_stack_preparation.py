"""A possible stack knowledge read marks its whole parent before provider work."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.core import pg, request_work
from server.core.diagnostics import codes_only
from server.knowledge.app import search, source_access
from server.memory.api.models_lifecycle import MemoryStackInput
from server.memory.app import stack


@pytest.fixture
def world(monkeypatch):
    token = request_work._current.set(None)
    request_work.begin()
    events = []
    state = {"marked": True, "recent": "", "queries": []}

    async def prepare(org, namespaces):
        events.append(("prepare", list(namespaces)))
        if state["marked"]:
            assert request_work.mark_mirrored()
        return source_access.ReadPlan(org, marked=state["marked"])

    async def profile(_input, _fill):
        events.append(("profile", codes_only()))

    async def pinned(_client, _input, _fill):
        events.append(("pinned", codes_only()))

    async def recent(_client, _input, _fill):
        events.append(("recent", codes_only()))
        return state["recent"]

    class Embedder:
        model_name = "fake"

        async def embed(self, seed):
            events.append(("embed", codes_only()))
            assert seed == "question" or seed == "recent seed"
            return [1.0]

    async def ranked(query):
        events.append(("query", codes_only()))
        state["queries"].append(query)
        return []

    monkeypatch.setattr(source_access, "prepare", prepare)
    monkeypatch.setattr(stack, "get_client", lambda: SimpleNamespace())
    monkeypatch.setattr(stack, "_stack_profile", profile)
    monkeypatch.setattr(stack, "_stack_pinned", pinned)
    monkeypatch.setattr(stack, "_stack_recent", recent)
    monkeypatch.setattr("server.embedding.get_embedder", Embedder)
    monkeypatch.setattr(search, "hybrid_search", ranked)
    monkeypatch.setattr(pg, "enabled", lambda: False)
    yield SimpleNamespace(events=events, state=state)
    request_work._current.reset(token)


def request(**changes):
    return MemoryStackInput(
        org="acme",
        namespace="docs",
        agent="reader",
        procedure_limit=0,
        include_knowledge=True,
        knowledge_top_k=2,
        **changes,
    )


@pytest.mark.parametrize("seed,recent", [("question", ""), ("", "recent seed"), ("", "")])
async def test_possible_knowledge_scope_is_prepared_before_the_first_stack_provider(
    world, seed, recent
):
    world.state["recent"] = recent
    output = await stack.handle_memory_stack(request(seed_query=seed))
    assert world.events[0] == ("prepare", ["docs"])
    assert all(private for name, private in world.events if name != "prepare")
    assert request_work.mirror_marked()
    assert output.entries == [] and output.tokens_used == 0
    names = [name for name, _ in world.events]
    assert names[:4] == ["prepare", "profile", "pinned", "recent"]
    if seed or recent:
        # This module fixes preparation order; the wider-read policy is separate.
        assert names[4] == "embed" and names.count("embed") == 1
        assert all(name == "query" for name in names[5:])
        assert names.count("query") == len(world.state["queries"])
        assert [query.top_k for query in world.state["queries"]] in ([2], [2, 8])
    else:
        assert names == ["prepare", "profile", "pinned", "recent"]
        assert world.state["queries"] == []
    for query in world.state["queries"]:
        assert query.namespaces == ("docs",)
        assert query.keys == query.tokens == []


async def test_unmarked_stack_keeps_the_semantic_query_and_ordinary_output(world):
    world.state["marked"] = False
    output = await stack.handle_memory_stack(request(seed_query="question"))
    assert output.entries == [] and output.tokens_used == 0
    assert not request_work.mirror_marked()
    assert all(not private for name, private in world.events if name != "prepare")
    assert [name for name, _ in world.events] == [
        "prepare",
        "profile",
        "pinned",
        "recent",
        "embed",
        "query",
    ]
    assert [query.top_k for query in world.state["queries"]] == [2]
    query = world.state["queries"][0]
    assert query.namespaces == ("docs",) and query.keys == query.tokens == []


async def test_disabled_knowledge_does_no_source_or_embedding_work(world):
    input = request(seed_query="question").model_copy(update={"include_knowledge": False})
    output = await stack.handle_memory_stack(input)
    assert [name for name, _ in world.events] == ["profile", "pinned", "recent"]
    assert all(not private for _, private in world.events)
    assert not world.state["queries"] and not request_work.mirror_marked()
    assert output.entries == [] and output.tokens_used == 0
