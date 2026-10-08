"""memory.ask enumerate-then-count for count-shaped questions.

On MemBench Aggregative the reader errs with the evidence in hand: recall
1.0, count off by one, or the count lost to the answer-char cap after the
enumeration consumed it. Count mode asks the model
to list then count, and folds the ANSWER line to the front so the cap keeps
the count. CH-free: a fake LLM captures the prompt and returns a scripted
two-part reply.
"""

from __future__ import annotations

import asyncio

from server.memory.app.ask.question_shapes import is_count_intent
from server.memory.app.ask.synthesis import _synthesize, fold_count_answer
from server.memory.domain.query_intent import QueryIntent


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


_MEMS = [
    _Mem("event_finsight_duration", "FinSight 2024 lasted 14 days"),
    _Mem("event_fitgarden_duration", "FitGarden Fest lasted 14 days"),
    _Mem("event_fintech_lab_duration", "FinTech Lab lasted 15 days"),
    _Mem("event_short", "QuickMeet lasted 2 days"),
]
_REPLY = (
    "ITEMS:\n"
    "- FinSight 2024 (14 days) [event_finsight_duration]\n"
    "- FitGarden Fest (14 days) [event_fitgarden_duration]\n"
    "- FinTech Lab (15 days) [event_fintech_lab_duration]\n"
    "ANSWER: 3 events — option C [event_finsight_duration][event_fitgarden_duration]"
    "[event_fintech_lab_duration]"
)


def test_is_count_intent_shapes() -> None:
    assert is_count_intent(QueryIntent(query_shape="aggregational", op="count"))
    assert is_count_intent(QueryIntent(query_shape="aggregational"))
    assert not is_count_intent(QueryIntent(query_shape="aggregational", op="sum"))
    assert not is_count_intent(QueryIntent(op="count", stated_value_update=True))
    assert not is_count_intent(QueryIntent(query_shape="temporal"))
    assert not is_count_intent(None)


def test_fold_puts_answer_first_and_keeps_the_worksheets_citations() -> None:
    folded = fold_count_answer(_REPLY)
    assert folded.startswith("3 events — option C")
    # The enumeration's quoted lines are the worksheet, not the answer; the
    # memories it rested on stay cited.
    assert "FinSight 2024 (14 days)" not in folded
    assert "[event_finsight_duration]" in folded
    assert "ITEMS:" not in folded
    # A 200-char cap keeps the count and the letter.
    assert folded[:200].startswith("3 events — option C")


def test_fold_leaves_plain_text_alone() -> None:
    assert fold_count_answer("There are 3 events. [k]") == "There are 3 events. [k]"
    assert fold_count_answer("ANSWER:") == "ANSWER:"


def test_count_mode_prompt_and_budget() -> None:
    llm = _FakeLLM(_REPLY)
    out = asyncio.run(
        _synthesize(
            llm,
            "How many events lasted two weeks or longer?",
            _MEMS,
            max_answer_chars=200,
            intent=QueryIntent(query_shape="aggregational", op="count"),
        )
    )
    assert "ITEMS:" in llm.system and "ANSWER:" in llm.system
    assert llm.max_tokens >= 900  # room to enumerate (with verdicts) despite the 200-char cap
    assert out.startswith("3 events — option C")
    assert len(out) <= 200


def test_non_count_question_unchanged() -> None:
    llm = _FakeLLM("FinSight 2024 lasted 14 days. [event_finsight_duration]")
    out = asyncio.run(
        _synthesize(
            llm,
            "How long did FinSight 2024 last?",
            _MEMS,
            max_answer_chars=200,
            intent=QueryIntent(query_shape="single_fact"),
        )
    )
    assert "ITEMS:" not in llm.system
    assert llm.max_tokens == 66  # the pre-existing cap arithmetic
    assert out == "FinSight 2024 lasted 14 days. [event_finsight_duration]"


def test_count_rules_read_category_from_the_question_and_boundary_from_the_options() -> None:
    """MemBench obs-Aggregative-447: five of nine Houston events were dropped
    as 'companies' because the dialogue said 'based in'; 116/186/258: gold
    reads 'X or longer' as 'more than X' and the inclusive count matched no
    option. The options are evidence of the asker's reading."""
    from server.memory.app.ask.prompts import _COUNT_SYSTEM_RULES

    assert "NEVER valid failure reasons unless a memory states that category" in _COUNT_SYSTEM_RULES
    assert "'Craft Fest' / 'CraftFit Fest'" in _COUNT_SYSTEM_RULES  # near-names stay distinct
    assert "EXACTLY on the threshold" in _COUNT_SYSTEM_RULES
    assert "write 'boundary' after the letter" in _COUNT_SYSTEM_RULES
    # The boundary re-read comes BEFORE the closest-option fallback.
    assert _COUNT_SYSTEM_RULES.index("'boundary'") < _COUNT_SYSTEM_RULES.index("'closest'")


def test_fold_keeps_a_prose_line_beside_the_enumeration() -> None:
    reply = (
        "ITEMS:\n- 7 attendees at Nate's party [nate_party]\n"
        "The memories do not record Joanna hosting a gaming party.\n"
        "ANSWER: 7 attendees at Nate's (not Joanna's) party [nate_party]"
    )
    folded = fold_count_answer(reply)
    assert folded.startswith("7 attendees at Nate's (not Joanna's) party")
    assert "do not record Joanna hosting" in folded  # the premise correction is part of the answer
    assert "- 7 attendees" not in folded
