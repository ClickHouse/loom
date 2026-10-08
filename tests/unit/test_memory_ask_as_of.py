"""Unit tests for memory.ask temporal anchoring (the as_of replay anchor).

Testable without ClickHouse: drive _synthesize with a fake LLM that captures
the system prompt it was handed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from server.memory.app.ask.synthesis import _synthesize


class _Mem:
    def __init__(self) -> None:
        self.memory_key = "our_pr_107585"
        self.memory_type = "fact"
        self.content_excerpt = "PR 107585 is open, waiting on CI."
        self.valid_at = "2026-09-01"
        self.created_at = "2026-09-01"


class _FakeLLM:
    def __init__(self) -> None:
        self.system: str = ""

    async def complete(self, system: str, user: str, max_tokens: int = 0):
        self.system = system

        class _R:
            text = "PR 107585 is open. [our_pr_107585]"

        return _R()


def _run(coro):
    return asyncio.run(coro)


def test_as_of_anchor_injected_when_set():
    llm = _FakeLLM()
    _run(_synthesize(llm, "status of PR 107585", [_Mem()], as_of=datetime(2026, 9, 3, 12, 0, 0)))
    assert "2026-09-03" in llm.system
    assert "present moment" in llm.system
    assert "dated after" in llm.system


def test_no_as_of_no_temporal_clause():
    llm = _FakeLLM()
    _run(_synthesize(llm, "status of PR 107585", [_Mem()]))
    assert "present moment" not in llm.system
    # the base synthesis system prompt is otherwise unchanged
    assert "ONLY the stored memories" in llm.system
