"""Unit: negative-outcome escalation in handle_memory_outcome.

Below the threshold, a negative is counted but the memory is untouched. At the
threshold, it's auto-quarantined: a `rejected` version row on loom.memory and
on its vector row, no in-place UPDATE. Uses a stub CH; the threshold is a
settings knob (default 0 = off)."""

from __future__ import annotations

import pytest

import server.memory.app.ops as ops
from server.memory.api.models_temporal import MemoryOutcomeInput


@pytest.fixture(autouse=True)
def _row(monkeypatch):
    """The outcome resolves its row before it counts anything."""
    from types import SimpleNamespace

    import server.memory.store as memory_repo

    async def get_by_id(memory_id, **kw):
        return SimpleNamespace(memory_id=memory_id, namespace="n")

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)


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


class _QuarantineCH(_StubCH):
    """Also answers the reject's reads: the identity's live ids and its vector row."""

    async def query_all(self, sql, params=None):
        if "FROM loom.memory FINAL" in sql:
            return [{"memory_id": "mid-1"}]
        if "FROM loom.memory_embeddings FINAL" in sql:
            p = params or {}
            return [
                {
                    "org": o,
                    "namespace": n,
                    "agent": a,
                    "user_id": u,
                    "session_id": s,
                    "memory_key": k,
                    "memory_type": "semantic",
                    "embedding": [0.1, 0.2],
                    "embedding_dim": 2,
                    "status": "active",
                    "version": 2,  # copied at read + 1 since its memory row
                }
                for o, n, a, u, s, k in zip(
                    p["o"], p["n"], p["a"], p["u"], p["s"], p["k"], strict=True
                )
            ]
        return []


async def test_negative_escalation_quarantines_at_threshold(monkeypatch):
    from types import SimpleNamespace

    from server.memory.store import lifecycle
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 3)
    ch = _QuarantineCH(
        [
            {"n": 3},  # rejection count reaches threshold
            {"agent": "a", "user_id": "-", "memory_key": "k"},  # identity lookup
            {"evidence_count": 0},  # evidence readback
        ]
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    head = SimpleNamespace(
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="s",
        memory_key="k",
        status="active",
        version=1,
        invalid_at=None,
    )

    async def get_by_id(memory_id, **kw):
        return head

    def build(row, **overrides):
        keys = ("org", "namespace", "agent", "user_id", "session_id", "memory_key", "status")
        return {**{k: getattr(row, k) for k in keys}, "invalid_at": None, **overrides}

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", build)
    monkeypatch.setattr("server.memory.store.inserts.check_memory_rows", lambda rows: None)

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
    written = {t: rows for t, rows in ch.inserts if t in ("loom.memory", "loom.memory_embeddings")}
    (mem,) = written["loom.memory"]
    assert mem["status"] == "rejected" and mem["version"] > 2
    assert mem["rejection_reason"] == "auto-quarantine: 3 negative outcomes"
    (emb,) = written["loom.memory_embeddings"]
    assert emb["status"] == "rejected" and emb["memory_key"] == "k"
    assert emb["version"] == mem["version"]
    assert ch.executes == []  # no in-place UPDATE


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
    assert ch.executes == []


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
