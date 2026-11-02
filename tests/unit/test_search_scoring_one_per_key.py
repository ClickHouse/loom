"""Callers that read one item per key take the best hit of each: the search serves a row
per writer and session of a key."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from server.memory.app.search.scoring import one_per_key, per_key_fetch


def test_one_per_key_keeps_the_first_hit_of_each_key_in_its_namespace() -> None:
    a1, a2, b, other_ns = (
        NS(memory_key="a"),
        NS(memory_key="a"),
        NS(memory_key="b"),
        NS(memory_key="a", namespace="n2"),
    )
    assert one_per_key([a1, a2, b, other_ns], "n", 5) == [a1, b, other_ns]
    assert one_per_key([a1, a2, b, other_ns], "n", 1) == [a1]


def test_the_fetch_covers_the_rows_one_key_can_take(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)
    assert per_key_fetch(5) == 15 and per_key_fetch(400) == 500
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 0)  # no cap: no multiplier
    assert per_key_fetch(5) == 5
