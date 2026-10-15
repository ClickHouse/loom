"""Emotion at a moment: quote cues, map to a basic emotion,
answer the strongest. MemBench Emotion: 0 of 5 with evidence recall 1.0 —
the reader wrote "negative/distressed" and picked a letter at random."""

from __future__ import annotations

import asyncio

from server.memory.app.ask.question_shapes import is_affect_intent
from server.memory.app.ask.synthesis import _synthesize, fold_count_answer
from server.memory.domain.query_intent import QueryIntent


def test_affect_gate() -> None:
    assert is_affect_intent(
        "What is the most likely user sentiment around '2024-10-01 08:02' Tuesday"
    )
    assert is_affect_intent("How did I feel about the move last spring?")
    assert is_affect_intent("What was my mood on Friday?")
    assert not is_affect_intent("How many events last two weeks?")
    assert not is_affect_intent("Where does Maya live now?")


class _Mem:
    def __init__(self, key: str, text: str, date: str) -> None:
        self.memory_key = key
        self.memory_type = "fact"
        self.content_excerpt = text
        self.valid_at = date
        self.created_at = date


class _FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.system = ""
        self.max_tokens = 0

    async def complete(self, system: str, user: str, max_tokens: int = 0):
        self.system = system
        self.max_tokens = max_tokens

        class _R:
            text = self.reply

        return _R()


def test_affect_mode_prompt_and_fold() -> None:
    reply = (
        "ITEMS:\n- 2024-10-01: 'a joke at my expense', 'felt belittled' [user_feeling_belittled]\n"
        "CUES:\nbelittled → Sadness\n"
        "ANSWER: Sadness — option C [user_feeling_belittled]"
    )
    llm = _FakeLLM(reply)
    out = asyncio.run(
        _synthesize(
            llm,
            "What is the most likely user sentiment around '2024-10-01 08:04' Tuesday\nA) Fear\nB) Anger\nC) Sadness\nD) Disgust",
            [
                _Mem(
                    "user_feeling_belittled",
                    "User felt belittled by a joke at their expense",
                    "2024-10-01",
                )
            ],
            max_answer_chars=200,
            intent=QueryIntent(),
        )
    )
    assert "EMOTIONAL STATE" in llm.system and "CUES:" in llm.system and "→ Disgust" in llm.system
    assert llm.max_tokens >= 500
    assert out.startswith(
        "C) Sadness — option C"
    )  # the picker leads with the named option's letter
    assert "CUES:" not in out and "belittled → Sadness" not in out
    assert "[user_feeling_belittled]" in out


def test_count_and_preference_take_precedence() -> None:
    llm = _FakeLLM("ITEMS:\n- a\nANSWER: 2")
    asyncio.run(
        _synthesize(
            llm,
            "How many times did I feel anxious last month?",
            [_Mem("k", "x", "2024-01-01")],
            intent=QueryIntent(op="count", query_shape="aggregational"),
        )
    )
    assert "COUNT question" in llm.system and "EMOTIONAL STATE" not in llm.system


def test_fold_strips_cues_header() -> None:
    assert fold_count_answer("ITEMS:\n- a [k1]\nCUES:\nx → Fear\nANSWER: Fear") == "Fear [k1]"


def test_affect_mode_prefers_the_closest_minute() -> None:
    llm = _FakeLLM("ITEMS:\n- a\nANSWER: Anger")
    asyncio.run(
        _synthesize(
            llm,
            "What is the most likely user sentiment around '2024-10-01 08:10'?",
            [_Mem("k", "x", "2024-10-01T08:08:00")],
            intent=QueryIntent(),
        )
    )
    assert "CLOSEST to the asked time decides" in llm.system
    assert "names the gap" in llm.system


def test_affect_question_does_not_abstain_on_similarity_when_results_exist(monkeypatch) -> None:
    """obs-Emotion-126: zero extracted facts, dated chunks present; the MC
    query abstained below the similarity gate while the bare question answered
    Fear (gold). Time proximity is the relevance signal for affect questions."""
    from dataclasses import dataclass
    from typing import Any

    from server.memory.app import ask as ma

    class _Hit:
        def __init__(self, key: str) -> None:
            self.memory_key = key
            self.memory_type = "episodic"
            self.content_excerpt = "a sudden chill seeped through the trees"
            self.similarity = 0.2
            self.rank_score = 0.2
            self.valid_at = "2024-10-02T07:41:00"
            self.created_at = "2024-10-02T07:41:00"

    class _Retrieval:
        results = [_Hit("chunk:s0:1")]
        abstain = True
        abstain_reason = "top hit similarity 0.20 is below the abstain threshold 0.27"
        abstain_code = "below_threshold"
        embedding_model = "fake"

    @dataclass
    class _SI:
        org: str = "o"
        namespace: str = "n"
        agent: str = "*"
        user_id: Any = None
        scope: str = "private"
        top_k: int = 3
        query: str = "What is the most likely user sentiment around '2024-10-02 07:40' Wednesday"
        observation_date: Any = None
        run_id: Any = None

        def temporal_requested(self) -> bool:
            return False

    async def _search(_inp):
        return _Retrieval()

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_recency_weight", 0.0)
    monkeypatch.setattr(settings, "memory_ask_identifier_injection", False)
    res = asyncio.run(ma.ask(_SI(), llm=None, allow_synthesis=False))
    assert res.source == "retrieval_only" and res.abstain is False
    assert res.memory_keys == ["chunk:s0:1"]


def test_affect_cues_treat_smell_words_as_disgust_and_map_unoffered_emotions() -> None:
    """MemBench obs-Emotion-19: 'a lingering scent of neglect' is a Disgust
    cue (gold) that read as Sadness; 181: 'disappointment' with no Sadness
    option was answered Disgust once and Anger once — the nearest-kind map
    makes the fallback deterministic."""
    from server.memory.app.ask.prompts import _AFFECT_SYSTEM

    assert "'a scent of neglect'" in _AFFECT_SYSTEM
    assert "Disgust is never chosen without a smell, taste or revulsion cue" in _AFFECT_SYSTEM
    assert "plans unravelling, unfairness → Anger" in _AFFECT_SYSTEM
    # The nearest-kind map is a fallback only; a named option at the asked minute wins,
    # and the gap is a computed number (par-Emotion-126 read 08:11 as nearer than 08:02).
    assert "ONLY when no option names the emotion" in _AFFECT_SYSTEM
    assert "'gap 2 min'" in _AFFECT_SYSTEM
    # obs-Emotion-167: 'warmth/laughter' with no Happiness option was answered Disgust.
    assert "never maps to a negative option" in _AFFECT_SYSTEM
