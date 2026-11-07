"""The entity extractor's candidate read is a queue — fresh
keys (memories created in the last `recent_seconds`) on every pass, backlog
keys from a whole-table key scan that refills once per `full_scan_seconds`
or when the queue runs dry, and one phase-two read (type + value under FINAL
with the sort key in PREWHERE, emptiness re-checked) for exactly the keys
the pass works on."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.workers import entity_extractor as w
from server.settings import settings


def _key(i: int, ns: str = "n") -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": ns,
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": f"k{i}",
    }


class _CH:
    def __init__(self, answers: list[list[dict[str, Any]]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers = list(answers)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, dict(params or {})))
        return self._answers.pop(0) if self._answers else []


@pytest.mark.asyncio
async def test_phase_one_has_no_value_and_phase_two_reads_by_key_under_prewhere() -> None:
    ch = _CH([[_key(1)], [{**_key(1), "memory_type": "semantic", "value": "v", "created_at": 1}]])
    rows = await memory_repo.find_unentitied_memories(batch_size=50, ch=ch)
    p1, p1_params = ch.calls[0]
    assert "value" not in p1.split("FROM", 1)[0] and "FINAL" not in p1
    assert "HAVING argMax(empty(m.entities), version) = 1" in p1
    assert p1_params["lim"] == 50 and "since_s" not in p1_params
    p2, p2_params = ch.calls[1]
    assert "FROM loom.memory FINAL" in p2
    assert "PREWHERE (org, namespace, agent, user_id, session_id, memory_key)" in p2
    assert "empty(entities)" in p2.split("WHERE", 1)[1]
    assert p2_params["keys"] == [("o", "n", "a", "u", "s", "k1")]
    assert rows == [{**_key(1), "memory_type": "semantic", "value": "v"}]


@pytest.mark.asyncio
async def test_window_narrows_phase_one_and_no_keys_means_no_phase_two() -> None:
    ch = _CH([[]])
    assert await memory_repo.find_unentitied_keys(batch_size=10, since_seconds=7200, ch=ch) == []
    sql, params = ch.calls[0]
    assert "m.created_at > now() - toIntervalSecond({since_s:UInt32})" in sql
    assert params["since_s"] == 7200
    assert await memory_repo.read_unentitied_rows([], ch=ch) == []
    assert len(ch.calls) == 1


def _reset_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "_backlog_keys", w.deque())
    monkeypatch.setattr(w, "_backlog_filled_at", 0.0)
    monkeypatch.setattr(settings, "entity_extract_full_scan_seconds", 1800)
    monkeypatch.setattr(settings, "entity_extract_recent_seconds", 7200)
    monkeypatch.setattr(settings, "entity_extract_full_scan_keys", 5000)


class _Repo:
    """Fake repo: the windowed one-shot read answers with fresh rows, the
    whole-table key scan with backlog keys, phase two with a value per key."""

    def __init__(self, fresh: list[dict[str, Any]], backlog: list[dict[str, Any]]) -> None:
        self.fresh, self.backlog = fresh, backlog
        self.key_calls: list[dict[str, Any]] = []
        self.row_calls: list[list[dict[str, Any]]] = []

    async def find_unentitied_memories(self, **kw: Any) -> list[dict[str, Any]]:
        self.key_calls.append(kw)
        src = self.fresh if kw.get("since_seconds") else self.backlog
        return [
            {**k, "memory_type": "semantic", "value": "v"} for k in list(src)[: kw["batch_size"]]
        ]

    async def find_unentitied_keys(self, **kw: Any) -> list[dict[str, Any]]:
        self.key_calls.append(kw)
        return list(self.backlog)[: kw["batch_size"]]

    async def read_unentitied_rows(
        self, keys: list[dict[str, Any]], **kw: Any
    ) -> list[dict[str, Any]]:
        self.row_calls.append(list(keys))
        return [{**k, "memory_type": "semantic", "value": "v"} for k in keys]


def _patch(monkeypatch: pytest.MonkeyPatch, repo: _Repo) -> None:
    import server.memory.store as memory_repo

    for name in ("find_unentitied_memories", "find_unentitied_keys", "read_unentitied_rows"):
        monkeypatch.setattr(memory_repo, name, getattr(repo, name))


@pytest.mark.asyncio
async def test_first_pass_reads_fresh_then_fills_the_queue_and_later_passes_drain_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_queue(monkeypatch)
    repo = _Repo(fresh=[_key(0, "fresh")], backlog=[_key(i) for i in range(1, 8)])
    _patch(monkeypatch, repo)
    cfg = w.EntityExtractorConfig(batch_size=3)

    rows = await w._next_candidates(cfg)
    # pass 1: window scan + whole-table scan (queue empty), batch = 1 fresh + 2 backlog
    assert [k.get("since_seconds") for k in repo.key_calls] == [7200, None]
    assert repo.key_calls[1]["batch_size"] == 5000
    assert [r["memory_key"] for r in rows] == ["k0", "k1", "k2"]
    assert len(w._backlog_keys) == 5

    rows = await w._next_candidates(cfg)
    # pass 2: window scan only; the queue supplies the rest, the fresh key is not taken twice
    assert [k.get("since_seconds") for k in repo.key_calls[2:]] == [7200]
    assert [r["memory_key"] for r in rows] == ["k0", "k3", "k4"]
    assert repo.row_calls[-1] == [_key(3), _key(4)]  # phase two only for the backlog keys


@pytest.mark.asyncio
async def test_queue_refills_when_dry_before_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_queue(monkeypatch)
    repo = _Repo(fresh=[], backlog=[_key(1), _key(2)])
    _patch(monkeypatch, repo)
    cfg = w.EntityExtractorConfig(batch_size=2)
    assert [r["memory_key"] for r in await w._next_candidates(cfg)] == ["k1", "k2"]
    assert not w._backlog_keys
    repo.backlog = [_key(3)]
    assert [r["memory_key"] for r in await w._next_candidates(cfg)] == ["k3"]
    assert [k.get("since_seconds") for k in repo.key_calls] == [7200, None, 7200, None]


def test_backlog_due_follows_the_slow_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_queue(monkeypatch)
    assert w._backlog_due(now=10.0)  # empty queue
    w._backlog_keys.append(_key(1))
    monkeypatch.setattr(w, "_backlog_filled_at", 10.0)
    assert not w._backlog_due(now=1809.0)
    assert w._backlog_due(now=1810.0)
    monkeypatch.setattr(settings, "entity_extract_full_scan_seconds", 0)
    assert w._backlog_due(now=11.0)  # clock off: every pass is a full scan


@pytest.mark.asyncio
async def test_clock_off_reads_every_key_without_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_queue(monkeypatch)
    monkeypatch.setattr(settings, "entity_extract_full_scan_seconds", 0)
    repo = _Repo(fresh=[], backlog=[_key(1)])
    _patch(monkeypatch, repo)
    rows = await w._next_candidates(w.EntityExtractorConfig(batch_size=25))
    assert [k.get("since_seconds") for k in repo.key_calls] == [None]
    assert repo.key_calls[0]["batch_size"] == 25
    assert [r["memory_key"] for r in rows] == ["k1"]


@pytest.mark.asyncio
async def test_fresh_rows_the_authority_no_longer_serves_leave_their_slots_to_the_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Postgres a fresh row ClickHouse still shows but the authority does not (a
    parked delivery) takes no slot: the backlog fills the batch."""
    import server.memory.store as memory_repo

    _reset_queue(monkeypatch)
    stale = [_key(i, "stale") for i in range(3)]
    repo = _Repo(fresh=stale, backlog=[_key(i) for i in range(3, 6)])
    _patch(monkeypatch, repo)

    async def served_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in rows if r["namespace"] != "stale"]

    monkeypatch.setattr(memory_repo, "served_rows", served_rows)
    rows = await w._next_candidates(w.EntityExtractorConfig(batch_size=3))
    assert [r["memory_key"] for r in rows] == ["k3", "k4", "k5"]


def test_run_pass_draws_its_rows_from_the_queue() -> None:
    src = open(w.__file__).read()
    assert "rows = await _next_candidates(cfg)" in src
    assert "find_unentitied_memories(" not in src.split("async def run_pass", 1)[1]
    # the fresh read stays on the one-shot function so a fake of it still drives run_pass
    assert "memory_repo.find_unentitied_memories(" in src.split("async def _next_candidates", 1)[1]
