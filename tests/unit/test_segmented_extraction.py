"""Segmented extraction: turn grouping and per-segment extraction with index
offsets over size windows (not embedding-similarity topic cuts)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.memory.domain.topic_segment import turn_ranges


def _conv(*turns: tuple[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u, a in turns:
        out.append({"role": "user", "content": u})
        out.append({"role": "assistant", "content": a})
    return out


def test_turn_ranges_group_user_plus_following_assistant_messages() -> None:
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a1b"},
        {"role": "user", "content": "u2"},
    ]
    assert turn_ranges(msgs) == [(0, 1), (1, 4), (4, 5)]
    assert turn_ranges([]) == []


@dataclass
class _LLM:
    tokens_in: int = 100
    tokens_out: int = 10
    provider: str = "fake"
    model: str = "fake-1"
    cached_input_tokens: int = 0


@dataclass
class _Fact:
    memory_key: str
    value: str
    tags: list[str] = field(default_factory=list)
    source_message_indices: list[int] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    linked_keys: list[str] = field(default_factory=list)
    relations: list[Any] = field(default_factory=list)
    temporal_anchor: str = ""
    valid_at: str = ""
    valid_to: str = ""
    judge_verdict: str = ""


@dataclass
class _Extraction:
    facts: list[_Fact]
    llm_result: _LLM
    prompt_version: str = "v"
    dropped_judge_wrong: int = 0
    dropped_parse: int = 0


def test_llm_extract_step_runs_one_call_per_segment_and_offsets_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pipeline as mp
    from server.memory.app import pipeline_state as pipeline_state
    from server.settings import settings

    calls: list[tuple[int, list[dict[str, Any]] | None]] = []

    async def _extract(messages: list[dict[str, Any]], **kw: Any) -> _Extraction:
        calls.append((len(messages), kw.get("last_messages")))
        first = messages[0]["content"]
        return _Extraction(
            facts=[_Fact(memory_key=f"k_{first[:8]}", value=first, source_message_indices=[0, 1])],
            llm_result=_LLM(tokens_in=100, tokens_out=10),
            dropped_parse=1,
        )

    monkeypatch.setattr("server.memory.app.extract.extract_from_messages", _extract)
    # 20 messages = 10 turns: past the size cap, read in windows of 5 turns.
    monkeypatch.setattr(settings, "memory_extract_size_segment_messages", 12)
    monkeypatch.setattr(settings, "memory_extract_size_segment_turns", 5)
    monkeypatch.setattr(settings, "memory_extract_size_segment_chars", 0)

    cooking = [(f"cooking question {i}", f"cooking answer {i}") for i in range(5)]
    k8s = [(f"kubernetes question {i}", f"kubernetes answer {i}") for i in range(5)]
    msgs = _conv(*cooking, *k8s)

    # two windows -> two calls, indices shifted, tags added
    state = pipeline_state.WorkflowState(org="o", namespace="ns", messages=msgs)
    state = asyncio.run(mp._step_llm_extract(state, {}))
    assert state.segments == 2 and len(calls) == 2
    assert sorted(c[0] for c in calls) == [10, 10]
    assert any(
        c[1] and c[1][-1]["content"] == "cooking answer 4" for c in calls
    )  # tail carried over
    idxs = sorted(f["source_message_indices"] for f in state.extracted_facts)
    assert idxs == [[0, 1], [10, 11]]
    assert sorted(t for f in state.extracted_facts for t in f["tags"]) == ["segment:0", "segment:1"]
    assert state.tokens_in == 200 and state.tokens_out == 20 and state.dropped_parse == 2

    # size windows off -> one call, no segment tags
    monkeypatch.setattr(settings, "memory_extract_size_segment_messages", 0)
    calls.clear()
    state = pipeline_state.WorkflowState(org="o", namespace="ns", messages=msgs)
    state = asyncio.run(mp._step_llm_extract(state, {}))
    assert state.segments == 1 and len(calls) == 1 and calls[0][0] == 20
    assert all(not any(t.startswith("segment:") for t in f["tags"]) for f in state.extracted_facts)
