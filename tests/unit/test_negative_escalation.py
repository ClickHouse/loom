"""Unit: negative-outcome escalation in handle_memory_outcome.

Below the threshold, a negative is counted but the memory is untouched. At the
threshold, it's auto-quarantined: a `rejected` version row on loom.memory and
on its vector row, no in-place UPDATE. Uses a stub CH; the threshold is a
settings knob (default 0 = off)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server.memory.app.ops as ops
from server.memory.api.models_temporal import MemoryOutcomeInput


@pytest.fixture(autouse=True)
def _row(monkeypatch):
    """The outcome resolves its row before it counts anything."""
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
    """Also answers the reject's reads: the identity's live ids and their vector rows."""

    def __init__(self, query_one_returns, ids):
        super().__init__(query_one_returns)
        self.ids = ids

    async def query_all(self, sql, params=None):
        if "FROM loom.memory FINAL" in sql:
            return [{"memory_id": mid} for mid in self.ids]
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


def _session_row(session_id, lock_mode=""):
    return SimpleNamespace(
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id=session_id,
        memory_key="k",
        memory_id=f"mid-{session_id}",
        status="active",
        version=1,
        invalid_at=None,
        lock_mode=lock_mode,
        lock_until=None,
    )


def _crossing_threshold():
    return [
        {"n": 3},  # rejection count reaches the threshold
        {"agent": "a", "user_id": "-", "memory_key": "k"},  # identity lookup
        {"evidence_count": 0},  # evidence readback
    ]


def _wire_quarantine(monkeypatch, heads, named="mid-s1"):
    """`heads`: the live rows of the identity by memory id, as the reject reads them;
    `named` is the one the outcome names. The threshold is 3, which the count crosses."""
    import server.memory.store as memory_repo
    from server.memory.store import lifecycle
    from server.settings import settings

    ch = _QuarantineCH(_crossing_threshold(), ids=list(heads))

    async def get_by_id(memory_id, **kw):
        return heads[memory_id]

    async def get_named(memory_id, **kw):
        return heads[named]

    def build(row, **overrides):
        keys = ("org", "namespace", "agent", "user_id", "session_id", "memory_key", "status")
        return {**{k: getattr(row, k) for k in keys}, "invalid_at": None, **overrides}

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 3)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(memory_repo, "get_by_id", get_named)
    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", build)
    monkeypatch.setattr("server.memory.store.inserts.check_memory_rows", lambda rows: None)
    return ch


def _negative(memory_id="mid-s1"):
    return MemoryOutcomeInput(
        org="o", namespace="n", memory_id=memory_id, outcome="negative", weight=1
    )


async def test_negative_escalation_quarantines_at_threshold(monkeypatch):
    ch = _wire_quarantine(monkeypatch, {"mid-s1": _session_row("s1")})

    out = await ops.handle_memory_outcome(_negative())
    assert out.quarantined is True
    written = {t: rows for t, rows in ch.inserts if t in ("loom.memory", "loom.memory_embeddings")}
    (mem,) = written["loom.memory"]
    assert mem["status"] == "rejected" and mem["version"] > 2
    assert mem["rejection_reason"] == "auto-quarantine: 3 negative outcomes"
    (emb,) = written["loom.memory_embeddings"]
    assert emb["status"] == "rejected" and emb["memory_key"] == "k"
    assert emb["version"] == mem["version"]
    assert ch.executes == []  # no in-place UPDATE


@pytest.mark.parametrize("mode", ["read_only", "no_delete"])
async def test_a_locked_memory_is_not_quarantined(monkeypatch, mode):
    # Its unlocked copy in another session stays too.
    heads = {"mid-s1": _session_row("s1", lock_mode=mode), "mid-s2": _session_row("s2")}
    ch = _wire_quarantine(monkeypatch, heads)

    out = await ops.handle_memory_outcome(_negative())
    assert out.recorded is True and out.quarantined is False
    assert [t for t, _ in ch.inserts] == ["loom.memory_usage_events"]


async def test_a_quarantine_leaves_a_locked_copy_in_another_session(monkeypatch):
    heads = {"mid-s1": _session_row("s1"), "mid-s2": _session_row("s2", lock_mode="read_only")}
    ch = _wire_quarantine(monkeypatch, heads)

    out = await ops.handle_memory_outcome(_negative())
    assert out.quarantined is True
    written = {t: rows for t, rows in ch.inserts if t in ("loom.memory", "loom.memory_embeddings")}
    for table in ("loom.memory", "loom.memory_embeddings"):
        assert [(r["session_id"], r["status"]) for r in written[table]] == [("s1", "rejected")], (
            table
        )


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
