"""memory.search graph mode: entity expansion and query-triple matching run
side by side (the stage costs the longer of the two, not their sum)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.app.search import graph as ms


@pytest.mark.asyncio
async def test_graph_expansion_and_triple_matching_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    marks: list[tuple[str, str]] = []

    async def expansion(ctx: Any) -> None:
        marks.append(("expansion", "start"))
        await asyncio.sleep(0.01)
        marks.append(("expansion", "end"))

    async def triples(ctx: Any) -> None:
        marks.append(("triples", "start"))
        await asyncio.sleep(0.01)
        marks.append(("triples", "end"))

    monkeypatch.setattr(ms, "_run_graph_expansion", expansion)
    monkeypatch.setattr(ms, "_run_triple_matching", triples)
    await ms._run_graph_stages(NS(input=NS()))
    # Both started before either finished.
    assert marks[:2] == [("expansion", "start"), ("triples", "start")]


@pytest.mark.asyncio
async def test_graph_stages_return_immediately_outside_graph_mode() -> None:
    ctx = NS(input=NS(search_mode="semantic"))
    await ms._run_graph_stages(ctx)  # neither stage touches the context
    assert not hasattr(ctx, "query_triples")


@pytest.mark.parametrize(
    "bounds",
    [
        {"user_id": "reader"},
        {"user_id": "-"},
        {"session_id": "session"},
        {"session_ids": ["first", "second"]},
        {"run_id": "run"},
        {"scope": "private", "agent": "writer"},
    ],
)
async def test_identity_bounded_graph_reads_do_not_consult_namespace_evidence(
    monkeypatch: pytest.MonkeyPatch, bounds: dict[str, Any]
) -> None:
    def forbidden() -> None:
        pytest.fail("a bounded graph read reached namespace-wide evidence")

    monkeypatch.setattr(ms, "get_client", forbidden)
    ctx = NS(
        input=NS(search_mode="graph", **bounds),
        llm_free=False,
        effective_entity_filter=["topic"],
    )
    await ms._run_graph_stages(ctx)
    assert not hasattr(ctx, "query_triples")
