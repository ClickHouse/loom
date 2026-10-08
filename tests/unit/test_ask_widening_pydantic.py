"""memory.ask widens retrieval on the input the handler actually passes.

The handler builds a pydantic `MemorySearchInput`. A widening step (over-fetch
to the synthesis window, compound-question parts, multi-hop decomposition)
that used `dataclasses.replace` would raise on a pydantic model and be caught
fail-open, so the step would silently never run while dataclass stand-ins
kept unit tests green. These tests go through the real model.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app import ask as memory_answers
from server.memory.app.ask import retrieval_legs
from tests.unit.test_memory_ask import _FakeLLM, _hit, _run, _search_output


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The handler-path environment of test_memory_ask: faked search (recording
    every input it receives), faked LLM, cost cap off."""
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget

    e = SimpleNamespace(
        search_output=_search_output([]), search_inputs=[], llm=_FakeLLM("Blue [m1].")
    )

    async def fake_search(search_input: Any):
        e.search_inputs.append(search_input)
        return e.search_output

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    return e


def test_with_copies_pydantic_and_dataclass_inputs() -> None:
    si = MemorySearchInput(org="o", namespace="n", agent="*", query="q", top_k=15)
    out = retrieval_legs._with(si, top_k=30)
    assert isinstance(out, MemorySearchInput) and out.top_k == 30 and out.query == "q"
    assert si.top_k == 15  # a copy, not a mutation
    assert retrieval_legs._with(si, query="part", top_k=6).query == "part"

    @dataclass
    class _DC:
        query: str = "q"
        top_k: int = 10

    d = retrieval_legs._with(_DC(), top_k=3)
    assert isinstance(d, _DC) and d.top_k == 3


@pytest.mark.asyncio
async def test_over_fetch_reaches_retrieval_on_the_pydantic_input(env, monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_context_memories", 25)
    monkeypatch.setattr(settings, "memory_ask_recency_weight", 0.2)
    env.search_output = _search_output([_hit("m1", excerpt="favorite color: blue")])
    await _run(top_k=15)
    first = env.search_inputs[0]
    assert isinstance(first, MemorySearchInput)
    assert first.top_k == 30  # min(max(15*2, 25), 40): the pool, not the caller's 15


@pytest.mark.asyncio
async def test_compound_question_retrieves_its_second_part(env) -> None:
    env.search_output = _search_output([_hit("m1", excerpt="Alice is 30")])
    q = "How old is Alice? Additionally, how old is Bob?"
    await _run(query=q)
    queries = [getattr(s, "query", "") for s in env.search_inputs]
    assert len(queries) >= 2, queries
    assert any("Bob" in x and "Alice" not in x for x in queries), queries


@pytest.mark.asyncio
async def test_decomposition_parts_are_retrieved_on_the_pydantic_input(monkeypatch) -> None:
    seen: list[tuple[str, int]] = []

    async def _search(inp: Any):
        seen.append((inp.query, inp.top_k))
        return _search_output([_hit("k_" + str(abs(hash(inp.query)) % 10_000), excerpt="x")])

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    llm = _FakeLLM("When did Caroline adopt her dog?\nWhen did Caroline move to Boston?")
    q = "How long after Caroline adopted her dog did she move to Boston?"
    si = MemorySearchInput(org="o", namespace="n", agent="*", query=q, top_k=15)
    parts, merged = await retrieval_legs.decomposed_retrieval(
        si, q, [], llm=llm, mode="always", part_top_k=6
    )
    assert len(parts) == 2 and len(merged) == 2
    assert seen == [
        ("When did Caroline adopt her dog?", 6),
        ("When did Caroline move to Boston?", 6),
    ]


def test_no_dataclass_replace_is_applied_to_the_search_input() -> None:
    from pathlib import Path

    package = Path(memory_answers.__file__).parent
    src = "".join(p.read_text(encoding="utf-8") for p in sorted(package.glob("*.py")))
    assert "_dc_replace" not in src
    assert "dataclasses import replace as" not in src
    # over-fetch, compound parts, decomposition parts
    assert src.count("_with(search_input,") == 3
