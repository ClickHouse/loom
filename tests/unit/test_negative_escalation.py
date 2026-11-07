"""Unit: negative-outcome escalation in handle_memory_outcome.

Below the threshold, a negative is counted but the memory is untouched. At the
threshold, an unlocked memory is auto-quarantined: a `rejected` version row on
loom.memory and on its vector row, no in-place UPDATE. A locked one, or a locked copy
in another session, is left as it is. Uses a stub CH; the threshold is a settings
knob (default 0 = off)."""

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
        return SimpleNamespace(
            memory_id=memory_id,
            namespace="n",
            agent="a",
            user_id="-",
            memory_key="k",
            lock_mode="",
            lock_until=None,
        )

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
    """A count of 3 rejections, the named row's identity, and the reject's reads: the
    identity's live ids and their vector rows."""

    def __init__(self, ids):
        super().__init__([])
        self.ids = ids

    async def query_one(self, sql, params=None):
        if "event_type = 'rejection'" in sql:
            return {"n": 3}
        if "FROM loom.memory FINAL" in sql:
            return {"agent": "a", "user_id": "-", "memory_key": "k"}
        return {"evidence_count": 0}

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


def _session_row(session_id, lock_mode="", version=1):
    return SimpleNamespace(
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id=session_id,
        memory_key="k",
        memory_id=f"mid-{session_id}",
        status="active",
        version=version,
        invalid_at=None,
        lock_mode=lock_mode,
        lock_until=None,
    )


def _wire_quarantine(monkeypatch, heads, outcome_reads=None):
    """`heads`: the live rows of the identity by memory id, as the reject reads them;
    `outcome_reads` what the outcome read of them first, when that differs. The
    threshold is 3, which the count reaches."""
    import server.memory.store as memory_repo
    from server.memory.store import lifecycle
    from server.settings import settings

    ch = _QuarantineCH(ids=list(heads))
    first = outcome_reads or heads

    async def get_by_id(memory_id, **kw):
        return heads[memory_id]

    async def get_first(memory_id, **kw):
        return first[memory_id]

    def build(row, **overrides):
        keys = ("org", "namespace", "agent", "user_id", "session_id", "memory_key", "status")
        return {**{k: getattr(row, k) for k in keys}, "invalid_at": None, **overrides}

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 3)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(memory_repo, "get_by_id", get_first)
    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", build)
    monkeypatch.setattr("server.memory.store.inserts.check_memory_rows", lambda rows: None)
    return ch


def _negative():
    return MemoryOutcomeInput(
        org="o", namespace="n", memory_id="mid-s1", outcome="negative", weight=1
    )


@pytest.mark.parametrize("ahead", [0, 10**9])
async def test_negative_escalation_quarantines_at_threshold(monkeypatch, ahead):
    """`ahead`: the head's writer's clock runs ahead, and the rejected row goes above it."""
    from server.memory.store.rows import next_version

    head = _session_row("s1", version=next_version() + ahead if ahead else 1)
    ch = _wire_quarantine(monkeypatch, {"mid-s1": head})

    out = await ops.handle_memory_outcome(_negative())
    assert out.quarantined is True
    written = {t: rows for t, rows in ch.inserts if t in ("loom.memory", "loom.memory_embeddings")}
    (mem,) = written["loom.memory"]
    assert mem["status"] == "rejected" and mem["version"] > max(2, head.version)
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


async def test_a_lock_taken_after_the_outcome_read_still_holds(monkeypatch):
    # Unlocked when the outcome read it, locked by the time the reject reads it; its
    # unlocked copy in another session is rejected, but the named memory was not.
    ch = _wire_quarantine(
        monkeypatch,
        {"mid-s1": _session_row("s1", lock_mode="read_only"), "mid-s2": _session_row("s2")},
        outcome_reads={"mid-s1": _session_row("s1")},
    )

    out = await ops.handle_memory_outcome(_negative())
    assert out.quarantined is False
    (rows,) = [rows for t, rows in ch.inserts if t == "loom.memory"]
    assert [(r["session_id"], r["status"]) for r in rows] == [("s2", "rejected")]


async def test_a_memory_deleted_after_the_outcome_read_is_not_quarantined(monkeypatch):
    # Gone by the time the reject reads the identity's rows; its copy is rejected.
    ch = _wire_quarantine(
        monkeypatch, {"mid-s2": _session_row("s2")}, outcome_reads={"mid-s1": _session_row("s1")}
    )

    out = await ops.handle_memory_outcome(_negative())
    assert out.quarantined is False
    (rows,) = [rows for t, rows in ch.inserts if t == "loom.memory"]
    assert [r["session_id"] for r in rows] == ["s2"]


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

    out = await ops.handle_memory_outcome(_negative())
    assert out.quarantined is False
    assert ch.executes == []


async def test_threshold_off_by_default_skips_counting(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 0)
    ch = _StubCH([{"evidence_count": 0}])  # only the evidence readback
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    out = await ops.handle_memory_outcome(_negative())
    assert out.quarantined is False
    assert ch.inserts == []  # no rejection event recorded when disabled


async def test_a_contended_quarantine_records_the_outcome_and_answers_not_quarantined(
    monkeypatch,
):
    """The outcome's usage events are in: a 503 asking for a retry would count it twice."""
    from server.memory.store import pg_memory
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 3)
    ch = _StubCH([{"n": 3}, {"evidence_count": 0}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def contended(*_a, **_k):
        raise pg_memory.AdmissionContendedError("the key's heads kept changing")

    monkeypatch.setattr(ops, "reject_by_identity", contended)
    out = await ops.handle_memory_outcome(
        MemoryOutcomeInput(org="o", namespace="n", memory_id="mid-1", outcome="negative")
    )
    assert out.recorded is True and out.quarantined is False


def _pg_reject(monkeypatch, passes):
    """The Postgres reject reading `passes` (each pass's heads), the first passes'
    commits contended: what each commit attempt was handed."""
    from server.core import pg
    from server.memory.store import lifecycle, pg_memory

    reads = iter(passes)
    commits: list[list[dict]] = []

    async def heads(*_a):
        return next(reads)

    async def insert(rows, read, **_k):
        commits.append(rows)
        if len(commits) < len(passes):
            raise pg_memory.AdmissionContendedError("a head moved")
        return len(rows)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "live_heads_of", heads)
    monkeypatch.setattr(lifecycle, "_hydrate", lambda h: SimpleNamespace(**h))
    monkeypatch.setattr(lifecycle, "build_memory_version_row", lambda h, **o: {**vars(h), **o})
    monkeypatch.setattr(lifecycle, "insert_versions_over_heads", insert)
    return commits


def _pg_head(session_id, lock_mode=""):
    return vars(_session_row(session_id, lock_mode=lock_mode))


async def test_under_postgres_a_reject_refuses_a_locked_head_before_any_commit(monkeypatch):
    from fastapi import HTTPException

    from server.memory.store import lifecycle

    commits = _pg_reject(monkeypatch, [[_pg_head("s1"), _pg_head("s2", lock_mode="no_delete")]])
    with pytest.raises(HTTPException) as e:
        await lifecycle.reject_by_identity("o", "n", "a", "-", "k", "spam")
    assert e.value.status_code == 423 and commits == []


@pytest.mark.parametrize(
    "second_pass",
    [
        pytest.param([_pg_head("s1", lock_mode="read_only"), _pg_head("s2")], id="locked"),
        pytest.param([_pg_head("s2")], id="deleted"),
    ],
)
async def test_under_postgres_a_head_changed_between_passes_is_not_quarantined(
    monkeypatch, second_pass
):
    """The first pass's commit is contended by a lock on, or a delete of, the named head;
    the pass that commits leaves it, so the quarantine says it was not rejected."""
    from server.settings import settings

    commits = _pg_reject(monkeypatch, [[_pg_head("s1"), _pg_head("s2")], second_pass])
    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 3)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _QuarantineCH(ids=[]))

    import server.memory.store as memory_repo

    async def first_read(memory_id, **kw):
        return _session_row("s1")

    monkeypatch.setattr(memory_repo, "get_by_id", first_read)
    out = await ops.handle_memory_outcome(_negative())
    assert out.recorded is True and out.quarantined is False
    assert [[r["session_id"] for r in rows] for rows in commits] == [["s1", "s2"], ["s2"]]
