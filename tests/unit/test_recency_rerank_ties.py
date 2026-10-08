"""memory.ask's recency re-rank: rows stamped the same instant tie."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from server.memory.app.ask.retrieval_legs import _recency_rerank

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _row(key: str, minutes: int, sim: float = 0.5) -> SimpleNamespace:
    return SimpleNamespace(memory_key=key, valid_at=T0 + timedelta(minutes=minutes), similarity=sim)


def test_rows_stamped_together_keep_retrieval_order() -> None:
    for seed in range(50):
        rows = [_row("a", 5), _row("b", 5), _row("c", 5), _row("old", 0)]
        # Fresh objects in a shuffled allocation order: addresses must not decide.
        random.Random(seed).shuffle(rows)
        want = [r.memory_key for r in rows if r.memory_key != "old"]
        got = [r.memory_key for r in _recency_rerank(rows, w=0.2)]
        assert got[:3] == want and got[3] == "old"


def test_distinct_stamps_keep_their_arrival_rank() -> None:
    rows = [_row("oldest", 0), _row("mid", 1), _row("newest", 2)]
    got = [r.memory_key for r in _recency_rerank(rows, w=0.2)]
    assert got == ["newest", "mid", "oldest"]
