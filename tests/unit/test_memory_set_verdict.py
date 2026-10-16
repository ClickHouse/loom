"""memory.set returns its verdict.

The write path runs `check_on_commit` on every set; the verdict, the peers
it was reached against and a `next` hint ride every response. `dry_run=True` runs the check and writes
nothing: no loom.memory row, no embedding row, no audit hash, no usage
event. A `merge` is the first-class noop: `noop=True`, the existing row
reinforced through the auto_reinforce evidence event.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from typing import Any

import pytest

import server.memory.app.handlers as mem_handlers
from server.memory.api.models_common import MemorySetPeer, set_next_hint, set_peer_excerpt
from server.memory.api.models_crud import MemorySetInput, MemorySetOutput
from server.memory.app import conflicts as cf
from server.memory.app import restore as _restore_mod
from server.memory.app import set_commit as set_commit
from server.memory.app import stack as _stack_mod
from server.memory.app import write as write_mod
from server.memory.domain.conflicts import ConflictPeer, ConflictResult
from server.memory.store import MemoryRow
from server.observability.event_args import project_result

_NOW = datetime(2026, 9, 14, 12, 0, 0)
_PEER_ID = "33333333-3333-3333-3333-333333333333"


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        """The graph-write marks a scheduled projection flushes are not the write under test."""
        if table not in ("loom.namespace_write_marks", "loom.graph_projections"):
            self.inserts.append((table, rows))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None


class _Embedder:
    model_name = "fake"
    dim = 4

    async def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]


class _Calls:
    def __init__(self) -> None:
        self.check: list[dict[str, Any]] = []
        self.marked: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.usage: list[dict[str, Any]] = []
        self.superseded: list[Any] = []


def _peer(**kw: Any) -> ConflictPeer:
    base: dict[str, Any] = {
        "memory_id": _PEER_ID,
        "memory_key": "deploy_day",
        "similarity": 0.91,
        "pinned": False,
        "canonical": "deploys happen on tuesday",
        "value": '"Deploys happen on Tuesday"',
        "valid_at": _NOW - timedelta(days=10),
    }
    base.update(kw)
    return ConflictPeer(**base)


def _existing_row(evidence_count: int = 1, reinforcement_count: int = 1) -> MemoryRow:
    return MemoryRow(
        memory_id=_PEER_ID,
        org="test",
        namespace="personal",
        agent="cursor",
        user_id="-",
        session_id="-",
        memory_key="deploy_day",
        memory_type="semantic",
        value="Deploys happen on Tuesday",
        confidence=0.9,
        version=100,
        pinned=False,
        created_at=_NOW - timedelta(days=10),
        expires_at=None,
        valid_at=_NOW - timedelta(days=10),
        evidence_count=evidence_count,
        reinforcement_count=reinforcement_count,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    verdict: ConflictResult,
    *,
    existing_by_id: MemoryRow | None = None,
) -> tuple[_CH, _Calls]:
    ch, calls = _CH(), _Calls()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr(mem_handlers, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr("server.memory.app.set_commit.get_embedder", mem_handlers.get_embedder)
    monkeypatch.setattr("server.memory.app.update.get_embedder", mem_handlers.get_embedder)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch, raising=False)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    import server.memory.app.blacklist_guard as _bl

    monkeypatch.setattr(_bl, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(cf, "get_client", lambda: ch, raising=False)

    async def _none(*_: Any, **__: Any) -> None:
        return None

    async def _by_id(*_: Any, **__: Any) -> MemoryRow | None:
        return existing_by_id

    monkeypatch.setattr("server.memory.store.get_by_key", _none)
    monkeypatch.setattr("server.memory.store.get_by_id", _by_id)

    async def _check(**kw: Any) -> ConflictResult:
        calls.check.append(kw)
        return verdict

    async def _mark(**kw: Any) -> None:
        calls.marked.append(kw)

    monkeypatch.setattr(cf, "check_on_commit", _check)
    monkeypatch.setattr(cf, "mark_peers_conflicted", _mark)

    async def _audit(event_type: str, **kw: Any) -> str:
        calls.audit.append({"event_type": event_type, **kw})
        return "hash"

    monkeypatch.setattr("server.observability.audit.emit", _audit)

    async def _usage(**kw: Any) -> None:
        calls.usage.append(kw)

    monkeypatch.setattr("server.memory.store.record_usage_event", _usage)

    async def _supersede(**kw: Any) -> Any:
        calls.superseded.append(kw)
        return 0

    monkeypatch.setattr("server.memory.app.key_supersession.supersede_same_key_rows", _supersede)
    monkeypatch.setattr(set_commit, "_project_set_entities_fail_open", _none)
    return ch, calls


def _input(**kw: Any) -> MemorySetInput:
    base: dict[str, Any] = {
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "memory_key": "release_day",
        "value": "Deploys happen on Thursday",
        "classify": False,
    }
    base.update(kw)
    return MemorySetInput(**base)


# --- dry run -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_returns_the_verdict_and_inserts_nothing(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, ConflictResult(verdict="conflict", peers=[_peer()]))
    out = await write_mod.handle_memory_set(_input(dry_run=True))
    assert ch.inserts == [], "a dry run must not insert into loom.memory or memory_embeddings"
    assert calls.audit == [], "a dry run leaves no audit hash row"
    assert calls.marked == [] and calls.superseded == [] and calls.usage == []
    assert len(calls.check) == 1, "the conflict check still runs"
    assert out.dry_run is True and out.noop is True and out.version == 0
    assert out.conflict_verdict == "conflict"
    assert [p.memory_key for p in out.peers] == ["deploy_day"]
    assert out.peers[0].excerpt == "Deploys happen on Tuesday"
    assert out.peers[0].similarity == 0.91 and out.peers[0].memory_id == _PEER_ID
    assert out.peers[0].valid_at == _NOW - timedelta(days=10)
    assert "memory.update(memory_key='deploy_day'" in out.next


@pytest.mark.asyncio
async def test_dry_run_on_a_clear_write_is_empty_handed_and_silent(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, ConflictResult(verdict="clear", peers=[]))
    out = await write_mod.handle_memory_set(_input(dry_run=True))
    assert ch.inserts == [] and calls.audit == []
    assert out.conflict_verdict == "clear" and out.peers == [] and out.next == ""
    assert out.noop is True and out.dry_run is True


@pytest.mark.asyncio
async def test_dry_run_against_a_pinned_peer_answers_rejected_not_409(monkeypatch) -> None:
    ch, _ = _wire(
        monkeypatch,
        ConflictResult(verdict="rejected", peers=[_peer(pinned=True)], pinned_blocker_id=_PEER_ID),
    )
    out = await write_mod.handle_memory_set(_input(dry_run=True))
    assert ch.inserts == []
    assert out.conflict_verdict == "rejected" and out.noop is True
    assert "pinned" in out.next and "memory.update(memory_key='deploy_day'" in out.next


@pytest.mark.asyncio
async def test_dry_run_merge_records_no_evidence_event(monkeypatch) -> None:
    ch, calls = _wire(
        monkeypatch,
        ConflictResult(
            verdict="merge", peers=[_peer(similarity=0.99)], existing_memory_id=_PEER_ID
        ),
        existing_by_id=_existing_row(),
    )
    out = await write_mod.handle_memory_set(_input(dry_run=True))
    assert ch.inserts == [] and calls.usage == [] and calls.audit == []
    assert out.conflict_verdict == "merge" and out.noop is True and out.memory_id == _PEER_ID


@pytest.mark.asyncio
async def test_dry_run_on_a_non_searchable_type_inserts_nothing(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, ConflictResult(verdict="clear", peers=[]))
    out = await write_mod.handle_memory_set(_input(memory_type="episodic", dry_run=True))
    assert ch.inserts == [] and calls.check == []
    assert out.conflict_verdict == "" and out.noop is True and out.dry_run is True


# --- real writes carry the verdict -------------------------------------------


@pytest.mark.asyncio
async def test_a_conflict_write_carries_verdict_peers_and_the_update_hint(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, ConflictResult(verdict="conflict", peers=[_peer()]))
    out = await write_mod.handle_memory_set(_input())
    tables = sorted(t for t, _ in ch.inserts)
    assert tables == ["loom.memory", "loom.memory_embeddings"]
    assert len(calls.marked) == 1 and len(calls.audit) == 1
    assert out.conflict_verdict == "conflict" and out.noop is False and out.dry_run is False
    assert out.version > 0
    assert [p.memory_key for p in out.peers] == ["deploy_day"]
    assert (
        out.next.startswith("verdict=conflict")
        and "memory.update(memory_key='deploy_day'" in out.next
    )


@pytest.mark.asyncio
async def test_a_clear_write_has_an_empty_next_and_no_peers(monkeypatch) -> None:
    ch, _ = _wire(monkeypatch, ConflictResult(verdict="clear", peers=[]))
    out = await write_mod.handle_memory_set(_input())
    assert len(ch.inserts) == 2
    assert (
        out.conflict_verdict == "clear" and out.peers == [] and out.next == "" and out.noop is False
    )


@pytest.mark.asyncio
async def test_a_merge_is_a_noop_with_an_evidence_event(monkeypatch) -> None:
    ch, calls = _wire(
        monkeypatch,
        ConflictResult(
            verdict="merge", peers=[_peer(similarity=0.99)], existing_memory_id=_PEER_ID
        ),
        existing_by_id=_existing_row(),
    )
    out = await write_mod.handle_memory_set(_input())
    assert ch.inserts == []
    assert calls.usage and calls.usage[0]["event_type"] == "evidence"
    assert calls.usage[0]["memory_id"] == _PEER_ID
    assert out.noop is True and out.conflict_verdict == "merge" and out.memory_id == _PEER_ID
    assert out.peers[0].memory_key == "deploy_day"
    assert out.next == "", "a first merge carries no hint"


@pytest.mark.asyncio
async def test_a_repeated_merge_names_memory_pin(monkeypatch) -> None:
    _, _ = _wire(
        monkeypatch,
        ConflictResult(
            verdict="merge", peers=[_peer(similarity=0.99)], existing_memory_id=_PEER_ID
        ),
        existing_by_id=_existing_row(evidence_count=3),
    )
    out = await write_mod.handle_memory_set(_input())
    assert "memory.pin(memory_key='deploy_day'" in out.next


@pytest.mark.asyncio
async def test_a_merge_without_cosine_peers_names_the_row_it_merged_into(monkeypatch) -> None:
    _, _ = _wire(
        monkeypatch,
        ConflictResult(verdict="merge", peers=[], existing_memory_id=_PEER_ID),
        existing_by_id=_existing_row(reinforcement_count=2),
    )
    out = await write_mod.handle_memory_set(_input())
    assert [p.memory_key for p in out.peers] == ["deploy_day"]
    assert out.peers[0].excerpt == "Deploys happen on Tuesday" and out.peers[0].similarity == 1.0
    assert "memory.pin" in out.next


@pytest.mark.asyncio
async def test_a_duplicate_write_keeps_duplicate_of_and_adds_the_verdict(monkeypatch) -> None:
    ch, _ = _wire(
        monkeypatch,
        ConflictResult(
            verdict="duplicate", peers=[_peer(similarity=0.98)], duplicate_of_id=_PEER_ID
        ),
    )
    out = await write_mod.handle_memory_set(_input())
    assert len(ch.inserts) == 2
    assert out.duplicate_of == _PEER_ID and out.conflict_verdict == "duplicate"
    assert out.next == "" and out.peers[0].memory_id == _PEER_ID


# --- shapes -------------------------------------------------------------------


def test_peers_are_closest_first_and_capped_at_five() -> None:
    peers = [
        _peer(memory_id=f"id{i}", memory_key=f"k{i}", similarity=0.85 + i / 100) for i in range(7)
    ]
    out = set_commit._set_peers(NS(verdict="conflict", peers=peers, refined_peers=[]))
    assert len(out) == 5
    assert [p.memory_key for p in out] == ["k6", "k5", "k4", "k3", "k2"]


def test_refined_peers_are_listed_once() -> None:
    p = _peer()
    out = set_commit._set_peers(NS(verdict="refines", peers=[p], refined_peers=[p]))
    assert [x.memory_id for x in out] == [_PEER_ID]


def test_excerpt_drops_json_quotes_and_clips() -> None:
    assert set_peer_excerpt('"Tuesday"') == "Tuesday"
    assert set_peer_excerpt("a  b\n c") == "a b c"
    long = set_peer_excerpt("x" * 500)
    assert len(long) == 200 and long.endswith("…")


def test_next_hint_is_empty_on_the_common_cases() -> None:
    peer = MemorySetPeer(memory_key="k", similarity=0.9)
    assert set_next_hint("clear", []) == ""
    assert set_next_hint("duplicate", [peer]) == ""
    assert set_next_hint("refines", [peer]) == ""
    assert set_next_hint("merge", [peer]) == ""
    assert set_next_hint("conflict", []) == ""
    assert "memory.update(memory_key='k'" in set_next_hint("conflict", [peer])
    assert "memory.pin(memory_key='k'" in set_next_hint("merge", [peer], repeated_merge=True)
    unlinked = set_next_hint("merge", [peer], relations_unlinked=True)
    # Restating them would merge again: no call is suggested.
    assert (
        unlinked == "verdict=merge: relations not linked: this merged into a row that is not yours."
    )
    both = set_next_hint("merge", [peer], repeated_merge=True, relations_unlinked=True)
    assert both.startswith(unlinked) and "memory.pin(memory_key='k'" in both
    assert both.count("verdict=merge") == 1


def test_the_noop_lands_on_the_agent_events_row() -> None:
    out = MemorySetOutput(version=0, conflict_verdict="merge", noop=True, memory_id=_PEER_ID)
    projected = project_result(out)
    assert projected["out_conflict_verdict"] == "merge"
    assert projected["out_noop"] == "true"


def test_bulk_rows_carry_the_verdict_peers_and_next() -> None:
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app.bulk_verdicts import apply_bulk_verdicts

    memory_rows = [
        {"memory_key": f"k{i}", "memory_id": f"id{i}", "status": "active"} for i in range(3)
    ]
    embedding_rows = [{"memory_key": f"k{i}", "status": "active"} for i in range(3)]
    results = [_BulkSetResult(memory_key=f"k{i}", version=100 + i) for i in range(3)]
    verdicts = {
        0: ConflictResult(verdict="conflict", peers=[_peer()]),
        1: ConflictResult(
            verdict="merge", peers=[_peer(similarity=0.99)], existing_memory_id=_PEER_ID
        ),
    }
    apply_bulk_verdicts(memory_rows, embedding_rows, [0, 1, 2], results, verdicts)
    assert results[0].conflict_verdict == "conflict"
    assert results[0].peers[0].memory_key == "deploy_day"
    assert "memory.update(memory_key='deploy_day'" in results[0].next
    assert (
        results[1].conflict_verdict == "merge" and results[1].version == 0 and results[1].next == ""
    )
    assert results[2].conflict_verdict == "" and results[2].peers == []


def test_set_input_dry_run_defaults_off_and_output_defaults_are_empty() -> None:
    assert _input().dry_run is False
    out = MemorySetOutput(version=1)
    assert out.conflict_verdict == "" and out.peers == [] and out.next == ""
    assert out.noop is False and out.dry_run is False
