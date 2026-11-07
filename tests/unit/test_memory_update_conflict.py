"""memory.update / update_by_id meet the namespace like memory.set.

The new value goes through check_on_commit BEFORE the version is written:
a pinned contradiction answers 409 and nothing lands; a conflict flags both
sides after the write; a restatement of another row is written as its
duplicate; a clear verdict resets a stale mark from the old value; the flag
off skips the check; the row's own key is excluded by the check itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_crud import MemoryUpdateInput
from server.memory.app import conflicts as cf
from server.memory.app import handlers as mem_handlers
from server.memory.app import key_supersession as ks
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.app import update as update_mod
from server.memory.domain.conflicts import ConflictPeer, ConflictResult
from server.memory.store import MemoryRow
from server.memory.store import conflict_candidates as conflict_candidates

_NOW = datetime.now(UTC).replace(tzinfo=None)
_ID_A = "22222222-2222-2222-2222-222222222222"
_ID_P = "44444444-4444-4444-4444-444444444444"


def _row(**overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": _ID_A,
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "deploy_day",
        "memory_type": "semantic",
        "value": "Deploys happen on Tuesday",
        "confidence": 0.8,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=30),
        "expires_at": None,
        "entities": [],
        "status": "conflicted",
        "conflict_with": [_ID_P],
    }
    base.update(overrides)
    return MemoryRow(**base)


def _peer(mid: str = _ID_P, pinned: bool = False) -> ConflictPeer:
    return ConflictPeer(
        memory_id=mid, memory_key="release_day", similarity=0.9, pinned=pinned, canonical="x"
    )


class _StubCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return [
            {
                "memory_id": _ID_A,
                "agent": "cursor",
                "user_id": "-",
                "session_id": "-",
                "version": 100,
                "status": "conflicted",
            }
        ]

    async def execute(self, *_: Any, **__: Any) -> None:
        return None


class _Calls:
    def __init__(self) -> None:
        self.check: list[dict[str, Any]] = []
        self.marked: list[dict[str, Any]] = []
        self.refined: list[dict[str, Any]] = []
        self.superseded: list[list[dict[str, Any]]] = []
        self.embedding_rows: list[dict[str, Any]] = []


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    def _wire(verdict: ConflictResult, row: MemoryRow | None = None) -> tuple[_StubCH, _Calls]:
        ch, calls = _StubCH(), _Calls()
        row = row or _row()
        monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
        monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
        monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
        monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
        monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

        async def _get_by_id(memory_id: str, **_: Any) -> MemoryRow | None:
            return row if memory_id == row.memory_id else None

        monkeypatch.setattr("server.memory.store.get_by_id", _get_by_id)

        async def _namespaces_of_id(_org: str, memory_id: str, *, strong: bool) -> list[str]:
            assert strong  # the update acts on the holder it finds
            return [row.namespace] if memory_id == row.memory_id else []

        monkeypatch.setattr("server.memory.store.namespaces_of_id", _namespaces_of_id)

        async def _enforce(**_: Any) -> None:
            return None

        monkeypatch.setattr("server.memory.app.blacklist_guard.enforce_on_insert", _enforce)

        async def _check(**kw: Any) -> ConflictResult:
            calls.check.append(kw)
            return verdict

        async def _mark(**kw: Any) -> str:
            calls.marked.append(kw)
            return "conflict"

        async def _refines(**kw: Any) -> None:
            calls.refined.append(kw)

        async def _supersede(*, new_rows: list[dict[str, Any]], **_: Any) -> int:
            calls.superseded.append(new_rows)
            return 0

        async def _emb_rows(rows: list[dict[str, Any]], **_: Any) -> None:
            calls.embedding_rows.extend(rows)

        async def _no_edges(*_: Any, **__: Any) -> None:
            return None

        # The new value's edges are the integration tests' to check.
        monkeypatch.setattr("server.memory.app.update.project_revised_edges", _no_edges)
        monkeypatch.setattr(cf, "check_on_commit", _check)
        monkeypatch.setattr(cf, "mark_peers_conflicted", _mark)
        monkeypatch.setattr(cf, "apply_refines", _refines)
        monkeypatch.setattr(ks, "supersede_same_key_rows", _supersede)
        monkeypatch.setattr("server.memory.store.insert_embedding_rows", _emb_rows)
        return ch, calls

    return _wire


def _input(**kw: Any) -> MemoryUpdateInput:
    base = {
        "org": "test",
        "namespace": "personal",
        "memory_key": "deploy_day",
        "value": "Deploys happen on Thursday",
    }
    base.update(kw)
    return MemoryUpdateInput(**base)


@pytest.mark.asyncio
async def test_clear_verdict_writes_an_active_version_and_resets_the_old_mark(wired) -> None:
    ch, calls = wired(ConflictResult(verdict="clear", peers=[]))
    out = await update_mod.handle_memory_update(_input())
    assert out.updated and out.conflict_verdict == "clear" and out.conflict_peers == 0
    assert out.duplicate_of == ""
    # The check saw the row's identity and key (own key excluded inside check_on_commit).
    kw = calls.check[0]
    assert kw["memory_key"] == "deploy_day" and kw["agent"] == "cursor"
    assert kw["namespace"] == "personal" and kw["new_value"] == '"Deploys happen on Thursday"'
    assert kw["new_embedding"], "the update's embedding is what the check compares"
    ((_, rows),) = ch.inserts
    assert rows[0]["status"] == "active" and rows[0]["conflict_with"] == []
    assert calls.embedding_rows[0]["status"] == "active"
    assert not calls.marked and not calls.refined
    # Same-key supersession ran with the value-carrying one-row batch.
    assert calls.superseded == [
        [{"memory_id": _ID_A, "memory_key": "deploy_day", "value": '"Deploys happen on Thursday"'}]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("landed", ["clear", "moved"])
async def test_a_conflict_the_mark_did_not_flag_answers_clear(
    wired, monkeypatch, landed: str
) -> None:
    # Nothing was flagged, so there is no conflict to report.
    wired(ConflictResult(verdict="conflict", peers=[_peer()]))

    # `clear`: every peer retired before the mark; `moved`: the new row did.
    async def _not_flagged(**_: Any) -> str:
        return landed

    monkeypatch.setattr(cf, "mark_peers_conflicted", _not_flagged)
    out = await update_mod.handle_memory_update(_input())
    assert out.updated and (out.conflict_verdict, out.conflict_peers) == ("clear", 0)


@pytest.mark.asyncio
async def test_conflict_verdict_flags_both_sides_after_the_write(wired) -> None:
    ch, calls = wired(ConflictResult(verdict="conflict", peers=[_peer()]))
    out = await update_mod.handle_memory_update(_input())
    assert out.updated and out.conflict_verdict == "conflict" and out.conflict_peers == 1
    assert ch.inserts, "the version is written before the marks"
    assert calls.marked == [
        {"org": "test", "new_memory_id": _ID_A, "peer_ids": [_ID_P], "namespace": "personal"}
    ]


@pytest.mark.asyncio
async def test_pinned_contradiction_answers_409_and_writes_nothing(wired) -> None:
    ch, calls = wired(
        ConflictResult(verdict="rejected", peers=[_peer(pinned=True)], pinned_blocker_id=_ID_P)
    )
    with pytest.raises(HTTPException) as ei:
        await update_mod.handle_memory_update(_input())
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "PINNED_CONFLICT"
    assert ei.value.detail["pinned_blocker_id"] == _ID_P
    assert ei.value.detail["memory_id"] == _ID_A
    assert ch.inserts == [] and calls.embedding_rows == []
    assert not calls.superseded


@pytest.mark.asyncio
async def test_merge_and_duplicate_are_written_as_a_duplicate_of_the_other_row(wired) -> None:
    ch, calls = wired(ConflictResult(verdict="merge", peers=[_peer()], existing_memory_id=_ID_P))
    out = await update_mod.handle_memory_update(_input())
    assert out.updated and out.conflict_verdict == "merge" and out.duplicate_of == _ID_P
    ((_, rows),) = ch.inserts
    assert rows[0]["status"] == "duplicate" and rows[0]["duplicate_of"] == _ID_P
    assert calls.embedding_rows[0]["status"] == "duplicate"

    ch, calls = wired(ConflictResult(verdict="duplicate", peers=[_peer()], duplicate_of_id=_ID_P))
    out = await update_mod.handle_memory_update(_input())
    assert out.conflict_verdict == "duplicate" and out.duplicate_of == _ID_P
    assert ch.inserts[0][1][0]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_refines_supersedes_the_refined_peers(wired) -> None:
    ch, calls = wired(ConflictResult(verdict="refines", peers=[_peer()], refined_peers=[_peer()]))
    out = await update_mod.handle_memory_update(_input())
    assert out.updated and out.conflict_verdict == "refines"
    assert ch.inserts[0][1][0]["status"] == "active"
    (kw,) = calls.refined
    assert kw["new_memory_id"] == _ID_A and kw["new_memory_key"] == "deploy_day"
    assert kw["namespace"] == "personal"
    assert not calls.marked


@pytest.mark.asyncio
async def test_non_searchable_type_has_nothing_to_compare(wired) -> None:
    ch, calls = wired(
        ConflictResult(verdict="conflict", peers=[_peer()]),
        row=_row(memory_type="episodic", status="active", conflict_with=[]),
    )
    out = await update_mod.handle_memory_update(_input())
    assert out.updated and out.conflict_verdict == "" and calls.check == []
    assert calls.embedding_rows == []


@pytest.mark.asyncio
async def test_update_by_id_reports_the_same_verdict(wired, monkeypatch) -> None:
    from server.memory.api.models_crud import MemoryUpdateByIdInput

    async def _in_scope(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr("server.auth.authz.row_in_scope", _in_scope)
    ch, calls = wired(ConflictResult(verdict="conflict", peers=[_peer()]))
    out = await update_mod.handle_memory_update_by_id(
        MemoryUpdateByIdInput(org="test", memory_id=_ID_A, value="Deploys happen on Friday")
    )
    assert out.updated and out.conflict_verdict == "conflict" and out.conflict_peers == 1
    assert calls.marked[0]["new_memory_id"] == _ID_A


def test_own_key_is_excluded_by_the_check_itself() -> None:
    """The row's earlier versions share its key; check_on_commit's candidate
    query excludes the caller's own key, so an update is never its own peer."""
    import inspect

    # The peer statement lives in `_peer_rows`.
    # Stage A (`_peer_candidates_sql`) carries the exclusion.
    src = (
        inspect.getsource(cf.check_on_commit)
        + inspect.getsource(cf._peer_rows)
        + inspect.getsource(conflict_candidates._peer_candidates_sql)
    )
    assert "memory_key != {self_key:String}" in src


@pytest.mark.asyncio
async def test_embedding_mirror_keeps_provenance_and_attestation(wired) -> None:
    from server.core.hashing import sha256_value

    mentioned = _NOW - timedelta(days=3)
    # An external row: the mirror's source_class feeds memory.ask's unverified-external filter.
    ch, calls = wired(
        ConflictResult(verdict="clear", peers=[]),
        row=_row(source_class="external", mentioned_at=mentioned, status="active"),
    )
    await update_mod.handle_memory_update(_input())
    emb = calls.embedding_rows[0]
    assert emb["source_class"] == "external" and emb["verified"] == 0
    assert emb["mentioned_at"] == mentioned

    # An attestation that binds the written bytes (the same value re-written) stays live ...
    same = "Deploys happen on Tuesday"
    ch, calls = wired(
        ConflictResult(verdict="clear", peers=[]),
        row=_row(
            source_class="asserted",
            verified_by="human:alice",
            verified_sha256=sha256_value(same),
            content_sha256=sha256_value(same),
        ),
    )
    await update_mod.handle_memory_update(_input(value=same, tags=["ops"]))
    assert calls.embedding_rows[0]["verified"] == 1
    assert calls.embedding_rows[0]["source_class"] == "asserted"

    # ... and one over the old bytes is stale once the value changes.
    ch, calls = wired(
        ConflictResult(verdict="clear", peers=[]),
        row=_row(
            verified_by="human:alice",
            verified_sha256=sha256_value("Deploys happen on Tuesday"),
            content_sha256=sha256_value("Deploys happen on Tuesday"),
        ),
    )
    await update_mod.handle_memory_update(_input())
    assert calls.embedding_rows[0]["verified"] == 0
