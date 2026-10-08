"""CH-free tests for bench/relevance_gate.py: tolerant verdict parsing and the floor.

`pytest bench/test_relevance_gate.py` (bench/ is outside the default testpaths).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.relevance_gate import (
    GATE_SYSTEM,
    apply_verdict,
    build_gate_user,
    parse_relevant,
)

# ---- parsing --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"relevant": [1, 3]}', {0, 2}),
        ('Sure — {"relevant":[2]} is my answer', {1}),
        ("[1, 2, 3]", {0, 1, 2}),
        ("1, 3", {0, 2}),
        ('{"relevant": []}', set()),
        ("none of them", set()),
        ('{"relevant": [0, 4, 99]}', {3}),  # out-of-range indices dropped
        ('{"relevant": ["2", "x"]}', {1}),
    ],
)
def test_parse_relevant_tolerant(text: str, expected: set[int]) -> None:
    assert parse_relevant(text, 4) == expected


@pytest.mark.parametrize("text", ["", "   ", "I cannot decide.", '{"other": 1}'])
def test_parse_relevant_unparseable_is_none(text: str) -> None:
    # None (not an empty set) so the caller fails OPEN.
    assert parse_relevant(text, 4) is None


def test_gate_user_prompt_numbers_rows_from_one_and_truncates() -> None:
    user = build_gate_user("what colour?", ["a" * 900, "blue"])
    assert "QUESTION: what colour?" in user
    assert "\n1. " in user and "\n2. blue" in user
    assert "a" * 601 not in user  # judge excerpt cap
    assert "JSON" in GATE_SYSTEM


# ---- floor ----------------------------------------------------------------


def _rows(n: int) -> list[Any]:
    return [
        SimpleNamespace(memory_key=f"m{i + 1}", content_excerpt=f"row {i + 1}") for i in range(n)
    ]


def test_apply_verdict_filters_and_keeps_rank_order() -> None:
    v = apply_verdict(_rows(5), {4, 0, 2}, min_keep=2)
    assert v.mode == "filtered"
    assert [m.memory_key for m in v.kept] == ["m1", "m3", "m5"]
    assert v.dropped_keys == ["m2", "m4"]
    assert v.judged == 5


def test_apply_verdict_floor_restores_top_ranked_rows() -> None:
    v = apply_verdict(_rows(5), {4}, min_keep=3)
    assert v.mode == "floor"
    # The judge's pick survives; the top two ranked rows fill the floor.
    assert [m.memory_key for m in v.kept] == ["m1", "m2", "m5"]


def test_apply_verdict_none_keeps_all_as_error() -> None:
    v = apply_verdict(_rows(3), None, min_keep=1)
    assert v.mode == "error" and len(v.kept) == 3 and v.dropped == []


def test_apply_verdict_empty_verdict_with_floor_covering_window_is_kept_all() -> None:
    v = apply_verdict(_rows(3), set(), min_keep=3)
    assert v.mode == "kept_all" and len(v.kept) == 3
