"""memory.ask's knowledge and recent-turns blocks are bounded by their settings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.app.ask import context_blocks as cb
from server.settings import settings


def _hit(i: int, chars: int) -> SimpleNamespace:
    return SimpleNamespace(content="x" * chars, source_uri=f"doc{i}", chunk_idx=0, title="")


def test_the_knowledge_block_cuts_each_passage_and_drops_the_lowest_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "memory_ask_knowledge_passage_max_chars", 100)
    monkeypatch.setattr(settings, "memory_ask_knowledge_block_max_chars", 250)
    text = cb.knowledge_block_text([_hit(1, 500), _hit(2, 50), _hit(3, 50)])
    lines = text.splitlines()[1:]
    assert lines[0].startswith("[knowledge:doc1#0]") and lines[0].endswith(" …[truncated]")
    assert len(lines[0]) < 130
    assert [ln.split("]")[0] for ln in lines] == ["[knowledge:doc1#0", "[knowledge:doc2#0"]


def test_the_recent_turns_block_keeps_the_newest_turns_within_its_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "memory_ask_recent_max_turns", 3)
    monkeypatch.setattr(settings, "memory_ask_recent_turn_max_chars", 100)
    monkeypatch.setattr(settings, "memory_ask_recent_block_max_chars", 150)
    turns = [{"role": "user", "content": f"turn {i} " + "y" * 200} for i in range(5)]
    lines = cb.recent_turns_block(turns).splitlines()[1:]
    assert len(lines) == 1 and lines[0].startswith("[user] turn 4")  # the newest, cut
    assert lines[0].endswith(" …[truncated]")
