"""Unit: negative-outcome escalation in handle_memory_outcome.

Below the threshold, a negative is counted but the memory is untouched. At the
threshold, it's auto-quarantined (status -> 'rejected' on both tables). Uses a
stub CH; the threshold is a settings knob (default 0 = off)."""

from __future__ import annotations

import server.memory.app.ops as ops
from server.memory.api.models_temporal import MemoryOutcomeInput


class _StubCH:
    def __init__(self, query_one_returns):
        self._q1 = list(query_one_returns)
        self.inserts: list = []
        self.executes: list = []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows))

    async def query_one(self, sql, params=None):
        return self._q1.pop(0) if self._q1 else None

    async def execute(self, sql, params=None):
        self.executes.append((sql, params))


async def test_negative_escalation_quarantines_at_threshold(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 3)
    ch = _StubCH(
        [
            {"n": 3},  # rejection count reaches threshold
            {"agent": "a", "user_id": "-", "memory_key": "k"},  # identity lookup
            {"evidence_count": 0},  # evidence readback
        ]
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    out = await ops.handle_memory_outcome(
        MemoryOutcomeInput(
            org="o",
            namespace="n",
            memory_id="mid-1",
            outcome="negative",
            weight=1,
        )
    )
    assert out.quarantined is True
    # Quarantine flips status on BOTH the memory and its embedding row.
    joined = " ".join(sql for sql, _ in ch.executes)
    assert "loom.memory" in joined and "rejected" in joined
    assert "loom.memory_embeddings" in joined


async def test_negative_below_threshold_does_not_quarantine(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 5)
    ch = _StubCH(
        [
            {"n": 2},  # below threshold
            {"evidence_count": 0},  # evidence readback
        ]
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    out = await ops.handle_memory_outcome(
        MemoryOutcomeInput(
            org="o",
            namespace="n",
            memory_id="mid-1",
            outcome="negative",
            weight=1,
        )
    )
    assert out.quarantined is False
    assert ch.executes == []  # no status UPDATE issued


async def test_threshold_off_by_default_skips_counting(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 0)
    ch = _StubCH([{"evidence_count": 0}])  # only the evidence readback
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    out = await ops.handle_memory_outcome(
        MemoryOutcomeInput(
            org="o",
            namespace="n",
            memory_id="mid-1",
            outcome="negative",
            weight=1,
        )
    )
    assert out.quarantined is False
    assert ch.inserts == []  # no rejection event recorded when disabled
