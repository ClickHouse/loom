"""memory.ask lays the grounding window out the way the question reads it
(chronological for temporal intent) and says what it left out."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.ask import rendering, synthesis
from server.memory.domain.query_intent import parse_query_intent


def _hit(key: str, valid_at: str, sim: float = 0.8) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"fact {key}",
        distance=1 - sim,
        similarity=sim,
        rank_score=sim,
        valid_at=valid_at,
    )


class _LLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text="ok [b]")


def test_evidence_order_is_rank_without_a_temporal_cue() -> None:
    mems = [_hit("a", "2026-03-01"), _hit("b", "2025-01-01")]
    out, mode = rendering.evidence_order(mems, parse_query_intent("what is my favorite color"))
    assert mode == "rank" and [m.memory_key for m in out] == ["a", "b"]
    assert rendering.evidence_order(mems, None)[1] == "rank"


def test_evidence_order_reads_the_question_when_the_grammar_abstains() -> None:
    mems = [_hit("a", "2026-03-01"), _hit("b", "2025-01-01")]
    assert rendering.evidence_order(mems, None, "how long did the fix take")[1] == "chronological"
    assert (
        rendering.evidence_order(mems, None, "what changed in Settings.cpp since March")[1]
        == "chronological"
    )
    assert (
        rendering.evidence_order(mems, None, "latest status of the keeper test")[1]
        == "reverse_chronological"
    )
    assert rendering.evidence_order(mems, None, "what do we know about PR 109741")[1] == "rank"
    # op=latest from the grammar reads newest-first too
    latest = SimpleNamespace(chronological_intent="", resolved_dates=(), op="latest")
    assert rendering.evidence_order(mems, latest)[1] == "reverse_chronological"


def test_evidence_order_goes_chronological_for_temporal_questions() -> None:
    mems = [_hit("a", "2026-03-01"), _hit("b", "2025-01-01"), _hit("c", "")]
    intent = SimpleNamespace(chronological_intent="", resolved_dates=(), op="date_span")
    out, mode = rendering.evidence_order(mems, intent)
    assert mode == "chronological" and [m.memory_key for m in out] == ["b", "a", "c"]
    intent_desc = SimpleNamespace(chronological_intent="desc", resolved_dates=(), op="")
    out, mode = rendering.evidence_order(mems, intent_desc)
    assert mode == "reverse_chronological" and [m.memory_key for m in out] == ["a", "b", "c"]


def test_elision_note_names_count_span_and_keys() -> None:
    assert rendering.elision_note([]) == ""
    note = rendering.elision_note(
        [_hit(f"k{i}", f"2026-0{1 + i % 9}-01") for i in range(10)], max_keys=3
    )
    assert note.startswith(
        "[… 10 more retrieved memories not shown, dated 2026-01-01 to 2026-09-01"
    )
    assert "keys: k0, k1, k2, …" in note


def test_synthesize_orders_prompt_and_appends_elision() -> None:
    llm = _LLM()
    mems = [_hit("a", "2026-03-01"), _hit("b", "2025-01-01")]
    intent = SimpleNamespace(chronological_intent="asc", resolved_dates=(), op="")
    asyncio.run(
        synthesis._synthesize(
            llm,
            "when did it change",
            mems,
            intent=intent,
            omitted=[_hit("z", "2024-06-01")],
        )
    )
    system, user = llm.calls[0]
    assert "CHRONOLOGICAL order" in system
    assert user.index("[2025-01-01] [b]") < user.index("[2026-03-01] [a]")
    assert (
        "[… 1 more retrieved memories not shown, dated 2024-06-01 to 2024-06-01; keys: z." in user
    )
    # no cue, nothing omitted: prompt unchanged in shape
    llm2 = _LLM()
    asyncio.run(synthesis._synthesize(llm2, "favorite color", mems))
    assert "CHRONOLOGICAL" not in llm2.calls[0][0] and "not shown" not in llm2.calls[0][1]
