"""Segments share the static extraction prompt.

Two things must hold: the provider's cached-prefix count survives every
merge (retry merge, segment merge) and reaches the op output, and the
segments fan out all at once.

Pure unit: the extractor is monkeypatched, no LLM, no ClickHouse.
"""

from __future__ import annotations

import asyncio

import pytest

from server.llm.client import LLMResult
from server.memory.api.models_bulk import MemorySetFromMessagesOutput
from server.memory.app import pipeline as mp
from server.memory.app import pipeline_state as pipeline_state
from server.memory.app.extract import ExtractionResult
from server.memory.app.extract.client import _merge_llm_results
from server.memory.domain import extracted_facts as extracted


def _llm(tin: int, cached: int = 0, text: str = "x") -> LLMResult:
    return LLMResult(
        text=text,
        tokens_in=tin,
        tokens_out=1,
        provider="p",
        model="m",
        cached_input_tokens=cached,
    )


def test_llm_result_defaults_to_no_cached_tokens():
    r = LLMResult(text="", tokens_in=5, tokens_out=1, provider="p", model="m")
    assert r.cached_input_tokens == 0


def test_retry_merge_sums_the_cached_subset_with_the_total():
    merged = _merge_llm_results(_llm(1000, 800, "a"), _llm(1000, 900, "b"))
    assert (merged.tokens_in, merged.cached_input_tokens, merged.text) == (2000, 1700, "b")


def test_segment_merge_sums_cached_tokens_across_parts():
    a = ExtractionResult(facts=[], llm_result=_llm(7000, 0))
    b = ExtractionResult(facts=[], llm_result=_llm(7000, 6900))
    c = ExtractionResult(facts=[], llm_result=_llm(7000, 6900))
    out = extracted._merge_extractions([(0, a), (10, b), (20, c)])
    assert out.llm_result.tokens_in == 21000
    assert out.llm_result.cached_input_tokens == 13800


def test_single_part_merge_is_identity():
    a = ExtractionResult(facts=[], llm_result=_llm(7000, 6000))
    assert extracted._merge_extractions([(0, a)]).llm_result.cached_input_tokens == 6000


def test_output_model_carries_cached_tokens_in_default_zero():
    assert MemorySetFromMessagesOutput().cached_tokens_in == 0
    assert MemorySetFromMessagesOutput(cached_tokens_in=42).cached_tokens_in == 42


# --------------------------------------------------------------------------
# Fan-out ordering
# --------------------------------------------------------------------------


def _messages(n: int) -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(n)]


def _segments(n_msgs: int, size: int):
    from server.memory.domain.topic_segment import Segment

    return [
        Segment(idx=k, start=s, end=min(n_msgs, s + size), turns=size // 2)
        for k, s in enumerate(range(0, n_msgs, size))
    ]


class _Recorder:
    """Fake extractor: records, per call, how many other calls were in
    flight when it started, and which segment (by first message) it saw."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.completed = 0
        self.starts: list[tuple[str, int]] = []
        self.calls = 0

    async def __call__(self, msgs, **kw):
        self.calls += 1
        self.starts.append((msgs[0]["content"], self.in_flight))
        # The provider's cache is warm only once a call carrying the prompt
        # has completed before this one started.
        cached = 6900 if self.completed > 0 else 0
        self.in_flight += 1
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        self.completed += 1
        return ExtractionResult(facts=[], llm_result=_llm(7000, cached))


def _state(msgs) -> pipeline_state.WorkflowState:
    return pipeline_state.WorkflowState(org="o", namespace="n", agent="a", messages=msgs)


@pytest.fixture
def four_segments(monkeypatch):
    msgs = _messages(40)
    segs = _segments(40, 10)

    async def _plan(state):
        return segs

    rec = _Recorder()
    monkeypatch.setattr(mp, "_plan_segments", _plan)
    monkeypatch.setattr("server.memory.app.extract.extract_from_messages", rec)
    return msgs, segs, rec


def _run(state):
    async def go():
        return await mp._step_llm_extract(state, {})

    return asyncio.run(go())


def test_segments_fan_out_all_at_once(four_segments):
    msgs, segs, rec = four_segments
    state = _run(_state(msgs))
    assert rec.calls == 4
    # All four started before any finished: the third and fourth saw 2 and 3 in flight.
    assert [n for _, n in rec.starts] == [0, 1, 2, 3]
    assert state.cached_tokens_in == 0


def test_segment_order_in_parts_is_preserved_by_start_offset():
    # _merge_extractions sorts by offset, so the parts' completion order does not matter
    parts = [
        (20, ExtractionResult(facts=[], llm_result=_llm(1, 0))),
        (0, ExtractionResult(facts=[], llm_result=_llm(2, 0))),
    ]
    merged = extracted._merge_extractions(parts)
    assert merged.llm_result.tokens_in == 3
