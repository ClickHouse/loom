"""Answer options that are lists are compared item by item."""

from __future__ import annotations

import asyncio

from server.memory.app.ask.question_shapes import _option_lines
from server.memory.app.ask.synthesis import _synthesize
from server.memory.domain.query_intent import QueryIntent


class _Mem:
    def __init__(self, key: str, text: str) -> None:
        self.memory_key = key
        self.memory_type = "fact"
        self.content_excerpt = text
        self.valid_at = "2026-09-01"
        self.created_at = "2026-09-01"


class _FakeLLM:
    def __init__(self) -> None:
        self.system = ""

    async def complete(self, system: str, user: str, max_tokens: int = 0):
        self.system = system

        class _R:
            text = "D"

        return _R()


def test_option_line_detection() -> None:
    assert _option_lines("What did you recommend?\nA) ['Henry V']\nB) ['Fruit']\nC) x\nD) y") == 4
    assert _option_lines("How old is Maya?") == 0
    assert _option_lines("Plan A) was fine") == 0  # not at line start


def test_options_rule_only_when_options_are_present() -> None:
    llm = _FakeLLM()
    asyncio.run(
        _synthesize(
            llm,
            "What movies, books and dishes have you recommended to me?\nA) ['Fruit', 'Banana Bread']\nB) ['Henry V (1989)', 'Banana Bread']\nC) ['Henry V (1989)', 'Fruit']\nD) ['Henry V (1989)', 'Fruit', 'Banana Bread']",
            [_Mem("k", "x")],
            intent=QueryIntent(),
        )
    )
    assert "compare each option against your findings item by item" in llm.system
    llm2 = _FakeLLM()
    asyncio.run(
        _synthesize(
            llm2, "What movies have you recommended to me?", [_Mem("k", "x")], intent=QueryIntent()
        )
    )
    assert "item by item" not in llm2.system
