"""memory.ask answer-budget: max_answer_chars scales max_tokens, adds a concise
instruction, and hard-truncates the answer. 0 = unbounded."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from server.memory.app.ask.synthesis import _synthesize


@dataclass
class _Mem:
    memory_key: str = "k"
    memory_type: str = "semantic"
    content_excerpt: str = "PR 1 is open"
    valid_at: object = None
    created_at: object = None


class _FakeLLM:
    def __init__(self) -> None:
        self.max_tokens: int | None = None
        self.system: str = ""

    async def complete(self, system, user, max_tokens=600):
        self.system = system
        self.max_tokens = max_tokens
        return type("R", (), {"text": "X" * 5000})()


def test_bounded_truncates_and_scales() -> None:
    llm = _FakeLLM()
    out = asyncio.run(_synthesize(llm, "q", [_Mem()], max_answer_chars=200))
    assert len(out) == 200
    assert llm.max_tokens is not None and llm.max_tokens <= 600
    assert "200 characters" in llm.system


def test_zero_leaves_the_answer_unbounded() -> None:
    llm = _FakeLLM()
    out = asyncio.run(_synthesize(llm, "q", [_Mem()], max_answer_chars=0))
    assert len(out) == 5000  # hard cap 6000, answer 5000 -> untouched
    assert llm.max_tokens == 600  # default window (15) -> the default budget
