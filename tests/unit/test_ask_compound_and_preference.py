"""Two-part count questions and preference tallies in memory.ask.

CH-free: fake LLM and stubbed retrieval."""

from __future__ import annotations

import asyncio
from typing import Any

from server.memory.app import ask as ma
from server.memory.app.ask.question_shapes import (
    is_preference_intent,
    split_compound_question,
)
from server.memory.app.ask.synthesis import _synthesize, fold_count_answer
from server.memory.domain.query_intent import QueryIntent


def test_compound_split_on_additionally_and_also() -> None:
    q = "How many events last for two weeks?Additionally, How many people in my family are aged 34?"
    assert split_compound_question(q) == [
        "How many events last for two weeks?",
        "How many people in my family are aged 34?",
    ]
    assert split_compound_question(
        "How many events are in Atlanta? Also, how many last a week?"
    ) == [
        "How many events are in Atlanta?",
        "how many last a week?",
    ]
    # a plain question, and an "also" mid-sentence, stay whole
    assert split_compound_question("How many people also live in Texas?") == [
        "How many people also live in Texas?"
    ]
    assert split_compound_question("") == []


def test_preference_intent_from_grammar_or_cue() -> None:
    assert is_preference_intent(QueryIntent(preference_attribute="genre"), "x")
    assert is_preference_intent(
        None, "According to the movies I mentioned, what kind of movies might I prefer to watch?"
    )
    assert is_preference_intent(None, "What is my favourite cuisine?")
    assert not is_preference_intent(None, "How old is Maya?")
    assert not is_preference_intent(QueryIntent(op="count"), "How many events are in Atlanta?")


class _Mem:
    def __init__(self, key: str, text: str) -> None:
        self.memory_key = key
        self.memory_type = "fact"
        self.content_excerpt = text
        self.valid_at = "2026-09-01"
        self.created_at = "2026-09-01"


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


def test_preference_mode_prompt_and_fold() -> None:
    reply = (
        "ITEMS:\n- Die Hard — action [m1]\n- Mad Max — action [m2]\n- Blade Runner — sci-fi [m3]\n"
        "TALLY:\naction 2, sci-fi 1\n"
        "ANSWER: Action — option B [m1][m2]"
    )
    llm = _FakeLLM(reply)
    out = asyncio.run(
        _synthesize(
            llm,
            "According to the movies I mentioned, what kind of movies might I prefer?\nA) Sci-Fi\nB) Action",
            [
                _Mem("m1", "watched Die Hard"),
                _Mem("m2", "watched Mad Max"),
                _Mem("m3", "watched Blade Runner"),
            ],
            max_answer_chars=200,
            intent=None,
        )
    )
    assert "PREFERENCE question" in llm.system and "TALLY:" in llm.system
    assert llm.max_tokens >= 500
    assert out.startswith("B) Action — option B")  # the picker leads with the named option's letter
    assert "TALLY:" not in out and "Die Hard — action" not in out


def test_count_rules_added_only_in_count_mode() -> None:
    llm = _FakeLLM("ITEMS:\n- a\nANSWER: 1")
    asyncio.run(
        _synthesize(
            llm,
            "How many events last two weeks?",
            [_Mem("k", "x")],
            intent=QueryIntent(op="count", query_shape="aggregational"),
        )
    )
    assert "inclusive when the question says 'or more'" in llm.system
    assert "'~600'" in llm.system and "BOTH counts" in llm.system
    llm2 = _FakeLLM("x")
    asyncio.run(
        _synthesize(
            llm2,
            "How old is Maya?",
            [_Mem("k", "x")],
            intent=QueryIntent(query_shape="single_fact"),
        )
    )
    assert "COUNT question" not in llm2.system and "PREFERENCE" not in llm2.system


def test_fold_strips_tally_header_too() -> None:
    assert fold_count_answer("ITEMS:\n- a [k1]\nTALLY:\nx 1\nANSWER: x") == "x [k1]"


class _Hit:
    def __init__(self, key: str) -> None:
        self.memory_key = key
        self.memory_type = "semantic"
        self.content_excerpt = key
        self.similarity = 0.5
        self.rank_score = 0.5
        self.valid_at = "2026-09-01"
        self.created_at = "2026-09-01"


class _Retrieval:
    def __init__(self, hits: list[Any]) -> None:
        self.results = hits
        self.abstain = False
        self.abstain_reason = ""
        self.abstain_code = ""
        self.embedding_model = "fake"


def test_compound_question_retrieves_each_part(monkeypatch) -> None:
    from dataclasses import dataclass

    @dataclass
    class _SI:
        org: str = "o"
        namespace: str = "n"
        agent: str = "*"
        user_id: Any = None
        scope: str = "private"
        top_k: int = 3
        query: str = "How many events last for two weeks?Additionally, How many people in my family are aged 34?"
        observation_date: Any = None
        valid_as_of: Any = None
        known_as_of: Any = None
        run_id: Any = None

        def temporal_requested(self) -> bool:
            return False

    seen: list[str] = []

    async def _search(inp):
        seen.append(inp.query)
        if inp.query.startswith("How many people"):
            return _Retrieval([_Hit("user_mom_age"), _Hit("user_dad_age")])
        return _Retrieval([_Hit("event_a_duration"), _Hit("event_b_duration")])

    async def _no_facets(**kw):
        return []

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    from server.memory import store as memory_repo

    monkeypatch.setattr(memory_repo, "find_by_key_facet", _no_facets)
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_recency_weight", 0.0)
    monkeypatch.setattr(settings, "memory_ask_identifier_injection", False)

    res = asyncio.run(ma.ask(_SI(), llm=None, allow_synthesis=False))
    assert (
        seen[0].startswith("How many events")
        and seen[1] == "How many people in my family are aged 34?"
    )
    keys = [h.memory_key for h in res.results]
    assert keys[:2] == ["event_a_duration", "event_b_duration"] and len(res.results) == 3
    # the whole pool holds both halves
    assert res.facets == ["duration", "age"] or set(res.facets) == {"duration", "age"}


def test_count_mode_asks_for_a_verdict_per_item() -> None:
    """Threshold semantics are judged per line, and unclear items are listed
    but not counted (the three obs Aggregative misses: 'a whole week' counted
    under 'a week or less')."""
    llm = _FakeLLM(
        "ITEMS:\n- a — qualifies\n- b — unclear: about a week\nANSWER: 1 (2 if the unclear ones count); A"
    )
    out = asyncio.run(
        _synthesize(
            llm,
            "How many events last a week or less?",
            [_Mem("k", "x")],
            intent=QueryIntent(op="count", query_shape="aggregational"),
        )
    )
    assert "'— qualifies', '— fails: <why>' or '— unclear: <why>'" in llm.system
    assert "number of '— qualifies' lines" in llm.system
    assert "N (N+M if the unclear ones count)" in llm.system
    assert out.startswith("1 (2 if the unclear ones count); A")


def test_a_long_run_of_joints_is_capped_and_keeps_its_tail() -> None:
    from server.memory.app.ask.question_shapes import COMPOUND_MAX_PARTS

    stem = "How many dogs? " + " ".join(f"Also, how many cats {i}?" for i in range(300))
    parts = split_compound_question(stem)
    assert len(parts) == COMPOUND_MAX_PARTS
    assert parts[0] == "How many dogs?"
    # nothing is dropped: the last part carries every joint past the cap
    assert parts[-1].endswith("how many cats 299?")
    assert split_compound_question("A? Also, B?", max_parts=1) == ["A? Also, B?"]
