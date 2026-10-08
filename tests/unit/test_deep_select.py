"""Deterministic descent for the raw read:
`deep_select=top` opens the top-ranked memories with no selection call."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.memory.app import deep_evidence as de


class _LLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *a, **k):
        self.calls += 1
        return SimpleNamespace(text='{"picks": [2]}')


class _CH:
    async def query_all(self, sql, params=None):
        return []


def _mem(k: str):
    return SimpleNamespace(memory_key=k, content_excerpt=f"about {k}", memory_type="semantic")


def test_top_selector_takes_the_ranked_head_without_an_llm_call():
    mems = [_mem("a"), _mem("b"), _mem("c"), _mem("d")]
    picked, fallback = asyncio.run(de.select_candidates(None, "q", mems, max_memories=2))
    assert [m.memory_key for m in picked] == ["a", "b"] and fallback is True


def test_llm_selector_calls_once_and_honours_its_pick():
    llm = _LLM()
    mems = [_mem("a"), _mem("b"), _mem("c")]
    picked, fallback = asyncio.run(de.select_candidates(llm, "q", mems, max_memories=2))
    assert llm.calls == 1 and [m.memory_key for m in picked] == ["c"] and fallback is False


def test_pass_with_no_llm_is_deterministic_and_fail_open():
    mems = [_mem("a"), _mem("b"), _mem("c")]
    out = asyncio.run(
        de.deep_evidence_pass(
            llm=None,
            question="q",
            candidates=mems,
            org="o",
            namespace="n",
            reason="requested",
            max_memories=2,
            max_chars_per_memory=500,
            max_chars_total=1000,
            ch=_CH(),
        )
    )
    assert out.selected_keys == ["a", "b"] and out.selector_fallback is True
    assert out.block == "" and out.turns == []


def test_wire_models_carry_the_selector():
    from pydantic import ValidationError

    from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput

    a = MemoryAskInput(org="o", namespace="n", agent="*", query="q")
    assert a.deep_select is None
    assert (
        MemoryAskInput(org="o", namespace="n", agent="*", query="q", deep_select="top").deep_select
        == "top"
    )
    with pytest.raises(ValidationError):
        MemoryAskInput(org="o", namespace="n", agent="*", query="q", deep_select="random")
    assert MemoryAskOutput().deep_selector == ""


def test_ask_threads_the_request_selector_and_the_setting(monkeypatch):
    import inspect

    from server.memory.app import ask as ma
    from server.memory.app.ask import cascade

    # The selector choice and the pass itself live in helpers shared by the
    # serial call and the pre-started task.
    # ask threads deep_select to the phase that pre-starts the pass and the
    # phase that runs it serially.
    assert "deep_select=deep_select" in inspect.getsource(ma.ask)
    for phase in (cascade._prepare_synthesis, cascade._deep_answer):
        src = inspect.getsource(phase)
        assert "_deep_selector_for(" in src and "_run_deep_pass(" in src
    sel = inspect.getsource(cascade._deep_selector_for)
    assert "memory_ask_deep_evidence_select" in sel and "memory_ask_temporal_deep_select" in sel
    run = inspect.getsource(cascade._run_deep_pass)
    assert 'llm=client if selector == "llm" else None' in run
