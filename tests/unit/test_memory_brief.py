"""Memory.brief composes memory.ask and reshapes it into a
task briefing — answer + evidence (with provenance tier) + explicit unknowns +
next reads. The ask is stubbed, so this exercises the shaping, not retrieval."""

from __future__ import annotations

import server.memory.app.ask.handler as ask_mod
from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput
from server.memory.api.models_brief import MemoryBriefInput
from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.brief import handle_memory_brief


def _hit(key: str, cls: str, sess: str = "", excerpt: str = "x") -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        source_class=cls,
        session_id=sess,
        temporal_anchor="2026-02-01",
    )


def _brief(monkeypatch, out: MemoryAskOutput, **kw):
    captured: dict = {}

    async def _fake_ask(inp: MemoryAskInput) -> MemoryAskOutput:
        captured["inp"] = inp
        return out

    monkeypatch.setattr(ask_mod, "handle_memory_ask", _fake_ask)
    import asyncio

    res = asyncio.run(
        handle_memory_brief(
            MemoryBriefInput(org="o", namespace="n", agent="a", query="what?", **kw)
        )
    )
    return res, captured


def test_brief_shapes_evidence_unknowns_and_next_reads(monkeypatch) -> None:
    out = MemoryAskOutput(
        answer="They likely prefer tea. [inferred]",
        abstain=False,
        grounding="mixed",
        memory_keys=["m1", "m3"],
        results=[
            _hit("m1", "extracted", "s1"),
            _hit("m3", "derived", "s2"),
            _hit("m9", "asserted"),
        ],
        cited_sessions=["s1", "s2"],
    )
    res, captured = _brief(monkeypatch, out)
    # composed ask is an orientation read (no reinforcement credit)
    assert captured["inp"].reinforce is False and captured["inp"].query == "what?"
    # evidence is the CITED rows, each carrying its provenance tier
    assert [e.memory_key for e in res.evidence] == ["m1", "m3"]
    assert {e.memory_key: e.source_class for e in res.evidence} == {
        "m1": "extracted",
        "m3": "derived",
    }
    assert res.grounding == "mixed" and res.answer.endswith("[inferred]")
    # an answered ask names no unknowns
    assert res.unknowns == []
    # next reads = the cited sessions to open
    assert res.next_reads == ["session:s1", "session:s2"]


def test_brief_abstention_names_the_reason(monkeypatch) -> None:
    out = MemoryAskOutput(
        answer="",
        abstain=True,
        abstain_reason="no memory names that person",
        memory_keys=[],
        results=[],
    )
    res, _ = _brief(monkeypatch, out)
    assert res.abstained is True and res.answer == ""
    assert res.unknowns == ["no memory names that person"]


def test_brief_evidence_falls_back_to_top_results_when_nothing_cited(monkeypatch) -> None:
    out = MemoryAskOutput(
        answer="",
        abstain=False,
        memory_keys=[],
        results=[_hit("m1", "extracted"), _hit("m2", "external")],
    )
    res, _ = _brief(monkeypatch, out)
    # no cited keys → surface the retrieved rows so the agent still sees anchors
    assert {e.memory_key for e in res.evidence} == {"m1", "m2"}
    assert any(e.source_class == "external" for e in res.evidence)
