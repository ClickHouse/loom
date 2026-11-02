"""context.pack: decision-typed hits render as their own tagged section, and
`only_sections` restricts the pack to what a per-prompt caller asked for.

One memory.search feeds two planes on the wire: facts stay in
<memories>, decisions leave it and carry their structured record."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.api.models_context import ContextPackInput
from server.memory.app.context_pack import handle_context_pack

_FACT = NS(
    namespace="ns",
    memory_key="m1",
    content_excerpt="deploys are Tue/Thu",
    source="fact",
    source_resource_id="",
    rank_score=0.9,
    temporal_anchor="",
    valid_at="",
    memory_type="fact",
)
_DECISION = NS(
    namespace="ns",
    memory_key="d1",
    content_excerpt="we chose clickhouse",
    source="fact",
    source_resource_id="",
    rank_score=0.8,
    temporal_anchor="",
    valid_at="",
    memory_type="decision",
)


def _search_stub(hits: list[Any]) -> Any:
    async def _h(*a: Any, **k: Any) -> Any:
        return NS(
            results=hits,
            abstain=False,
            abstain_reason="",
            retrieval_confidence=0.9,
            retrieval_risk="low",
            route_agreement=1.0,
        )

    return _h


def _dead_other_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(*a: Any, **k: Any) -> Any:
        return NS(
            rendered="",
            blocks=[],
            block_count=0,
            labels=[],
            section_count=0,
            entries=[],
            tokens_used=0,
            tokens_budget=0,
            truncated=False,
            matches=[],
        )

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.app.procedure.handle_procedure_match",
    ):
        monkeypatch.setattr(target, _empty)


def test_decision_hits_render_as_their_own_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _dead_other_sections(monkeypatch)
    monkeypatch.setattr(
        # d1 recorded in two sessions: one line
        "server.memory.app.search.handle_memory_search",
        _search_stub([_FACT, _DECISION, NS(**{**vars(_DECISION), "rank_score": 0.7})]),
    )

    async def _records(ch: Any, org: str, ns: str, keys: list[str]) -> dict[str, Any]:
        assert keys == ["d1"]
        return {
            "d1": {
                "scenario": "pick a store for agent memory",
                "outcome": "ClickHouse",
                "reasoning": "one engine for vectors, text and time",
                "realized_outcome": "p95 under budget",
                "success": True,
                "made_at": "2026-09-01T10:00:00",
            }
        }

    monkeypatch.setattr("server.memory.app.decision._decision_records", _records)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())

    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(org="o", namespace="ns", agent="a", query="which store")
        )
    )

    assert "decisions" in out.sections and "memories" in out.sections
    dec = out.sections["decisions"]
    assert "[ns/d1 by=unknown] 2026-09-01 pick a store for agent memory" in dec
    assert "→ decided: ClickHouse" in dec
    assert "(because one engine for vectors, text and time)" in dec
    assert "realized: p95 under budget (succeeded)" in dec
    assert out.section_meta["decisions"]["citations"] == ["decision:ns/d1"]
    # The decision left the memories block; the fact stayed.
    assert "d1" not in out.sections["memories"]
    assert "deploys are Tue/Thu" in out.sections["memories"]
    assert "<decisions>" in out.rendered
    labels = {b.section: b.label for b in out.blocks}
    assert labels["decisions"] == "governance.decisions"


def test_record_fetch_failure_falls_back_to_the_excerpt(monkeypatch: pytest.MonkeyPatch) -> None:
    _dead_other_sections(monkeypatch)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search_stub([_DECISION]))

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("records table missing")

    monkeypatch.setattr("server.memory.app.decision._decision_records", _boom)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())

    out = asyncio.run(
        handle_context_pack(ContextPackInput(org="o", namespace="ns", agent="a", query="q"))
    )
    assert out.sections["decisions"] == "- [ns/d1 by=unknown] we chose clickhouse"
    assert "memories" not in out.sections  # the only hit was a decision


def test_include_decisions_false_keeps_them_in_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    _dead_other_sections(monkeypatch)
    monkeypatch.setattr(
        "server.memory.app.search.handle_memory_search", _search_stub([_FACT, _DECISION])
    )

    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(org="o", namespace="ns", agent="a", query="q", include_decisions=False)
        )
    )
    assert "decisions" not in out.sections
    assert "we chose clickhouse" in out.sections["memories"]


def test_only_sections_skips_every_other_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _tracking(name: str) -> Any:
        async def _h(*a: Any, **k: Any) -> Any:
            calls.append(name)
            raise RuntimeError("should not have been called")

        return _h

    for target, name in (
        ("server.memory.app.working_memory.handle_working_memory_render", "working_memory"),
        ("server.memory.app.stack.handle_memory_stack", "memory_stack"),
        ("server.memory.app.profile.handle_profile_render", "profile"),
        ("server.knowledge.app.knowledge_search.handle_knowledge_search", "knowledge"),
        ("server.knowledge.app.conversations.handle_conversation_search", "conversations"),
    ):
        monkeypatch.setattr(target, _tracking(name))
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search_stub([_FACT]))

    async def _match(*a: Any, **k: Any) -> Any:
        calls.append("procedures")
        return NS(matches=[])

    monkeypatch.setattr("server.memory.app.procedure.handle_procedure_match", _match)

    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="q",
                include_knowledge=True,
                include_conversations=True,
                only_sections=["memories", "decisions", "procedures"],
            )
        )
    )
    assert calls == ["procedures"]
    assert list(out.sections) == ["memories"]


def test_only_sections_rejects_unknown_names() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ContextPackInput(org="o", namespace="ns", agent="a", only_sections=["impact"])  # type: ignore[list-item]


def test_a_decision_whose_reasoning_is_its_outcome_gives_no_because(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dead_other_sections(monkeypatch)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search_stub([_DECISION]))
    ban = "Client-side retries only; no server retry setting."

    async def _records(ch: Any, org: str, ns: str, keys: list[str]) -> dict[str, Any]:
        return {"d1": {"scenario": "where requests are retried", "outcome": ban, "reasoning": ban}}

    monkeypatch.setattr("server.memory.app.decision._decision_records", _records)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    out = asyncio.run(
        handle_context_pack(ContextPackInput(org="o", namespace="ns", agent="a", query="retries"))
    )
    dec = out.sections["decisions"]
    assert f"→ decided: {ban}" in dec and "because" not in dec
