"""loom_search hands the agent the reader block."""

from __future__ import annotations

import pytest

from server.transport import mcp_tools


def _hits() -> list[dict]:
    return [
        {
            "memory_key": "user_hobby",
            "content_excerpt": "Audrey likes hiking on weekends " * 20,
            "namespace": "ns",
            "explanation": "semantic",
        },
        {
            "memory_key": "user_pet",
            "content_excerpt": "Audrey has a dog named Max",
            "namespace": "ns",
        },
    ]


def test_block_replaces_the_clipped_hit_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = {
        "reader_block": "[2024-01-24 10:00] [user_hobby] (semantic) Audrey likes hiking on weekends\n\n"
        "[2024-01-20 09:00] [user_pet] (semantic) Audrey has a dog named Max",
        "reader_instructions": "You answer using ONLY the stored memories provided below.",
        "candidates_total": 2,
    }
    lines = mcp_tools._memory_section_lines(memory, _hits(), top_k=5)
    text = "\n".join(lines)
    assert "How to read the memories below: You answer using ONLY" in text
    assert "[2024-01-24 10:00] [user_hobby]" in text and "dog named Max" in text
    assert "(ns=ns)" not in text  # no key + 200-char lines


def test_paging_hint() -> None:
    memory = {"reader_block": "[d] [k] (t) x", "candidates_total": 124}
    text = "\n".join(mcp_tools._memory_section_lines(memory, _hits(), top_k=5))
    assert "How to read" not in text, "no rule without reader_instructions"
    assert "Memory hits (2 of 124; next_page serves the rest):" in text


def test_without_a_block_the_hit_lines_are_what_they_were() -> None:
    text = "\n".join(mcp_tools._memory_section_lines({}, _hits(), top_k=1))
    assert text.startswith("\nMemory hits:")
    assert "[user_hobby] (ns=ns)" in text and "user_pet" not in text  # top_k=1 honoured
    first = [ln for ln in text.split("\n") if "user_hobby" in ln][0]
    assert len(first) < 260  # 200-char clip plus the label
