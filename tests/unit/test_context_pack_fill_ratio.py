"""context.pack reports budget pressure: the rendered size
on the block estimator, its share of token_budget, and each section's
share of its own budget before trimming."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.api.models_context import ContextPackInput
from server.memory.app.context_pack import handle_context_pack


def _stub_everything_but_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _dead(*a: Any, **k: Any) -> Any:
        raise RuntimeError("not in this test")

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.app.procedure.handle_procedure_match",
        "server.knowledge.app.knowledge_search.handle_knowledge_search",
        "server.knowledge.app.conversations.handle_conversation_search",
    ):
        monkeypatch.setattr(target, _dead)


def _memories(n: int, width: int) -> Any:
    async def _h(*a: Any, **k: Any) -> Any:
        return NS(
            results=[
                NS(
                    namespace="ns",
                    memory_key=f"m{i}",
                    content_excerpt="x" * width,
                    source="fact",
                    source_resource_id="",
                    rank_score=0.9,
                    temporal_anchor="",
                    valid_at="",
                    memory_type="fact",
                )
                for i in range(n)
            ],
            abstain=False,
            abstain_reason="",
            retrieval_confidence=0.9,
            retrieval_risk="low",
            route_agreement=1.0,
        )

    return _h


def test_fill_ratio_tracks_the_rendered_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_everything_but_memories(monkeypatch)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _memories(3, 40))
    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(org="o", namespace="ns", agent="a", query="q", token_budget=2000)
        )
    )
    assert out.tokens_estimate == max(1, len(out.rendered) // 4)
    assert out.fill_ratio == round(out.tokens_estimate / 2000, 3)
    assert 0 < out.fill_ratio < 1
    meta = out.section_meta["memories"]
    assert meta["tokens_budget"] > 0
    assert 0 < meta["fill_ratio"] < 1
    assert meta["truncated"] is False


def test_section_fill_ratio_exceeds_one_when_the_body_was_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memories block is packed to its budget upstream, so the section
    writer is exercised with procedures, which it trims itself: 40 long
    matches against a 200-token overall budget."""

    async def _dead(*a: Any, **k: Any) -> Any:
        raise RuntimeError("not in this test")

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.app.search.handle_memory_search",
        "server.knowledge.app.knowledge_search.handle_knowledge_search",
        "server.knowledge.app.conversations.handle_conversation_search",
    ):
        monkeypatch.setattr(target, _dead)

    async def _match(*a: Any, **k: Any) -> Any:
        return NS(
            matches=[
                NS(
                    steps=["s" * 120] * 6,
                    namespace="ns",
                    memory_key=f"p{i}",
                    name=f"proc{i}",
                    situation="release " * 10,
                    status="authored",
                    fail_prone_steps=[],
                )
                for i in range(40)
            ]
        )

    monkeypatch.setattr("server.memory.app.procedure.handle_procedure_match", _match)
    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o", namespace="ns", agent="a", query="q", situation="release", token_budget=200
            )
        )
    )
    meta = out.section_meta["procedures"]
    assert meta["truncated"] is True
    assert meta["fill_ratio"] > 1.0
    # The pack-level ratio is of the RENDERED (trimmed) text, so it stays bounded.
    assert out.fill_ratio <= 1.1
