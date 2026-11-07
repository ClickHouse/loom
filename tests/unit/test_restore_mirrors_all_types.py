"""The restore paths mirror the embeddings row for EVERY
memory type, the way delete tombstones it.

A deleted episodic key's embeddings row is tombstoned, so a restore that
guarded its mirror with `is_searchable_type` and a non-empty `embedding`
would leave the restored key's row tombstoned; the same holds for
`_restore_superseded`'s re-emit. Neither guards: an embedded row and a dim-0
stub are revived alike, for episodic, profile and semantic rows; the mirror
stays fail-open. CH-free.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.api.models_crud import MemoryRestoreInput
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import restore as restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.domain.memory_types import is_searchable_type
from server.memory.store import EMBEDDING_PERSISTED_COLS, MemoryRow
from server.observability.budget_shape import current_budget_shape, note_budget_shape
from tests.unit.claims_fake import fake_key_locks
from tests.unit.memory_store import stub_status_before

_NOW = datetime.now(UTC).replace(tzinfo=None)
_TYPES = ("episodic", "profile", "semantic")


@pytest.fixture(autouse=True)
def _key_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Undo holds the key's write lock, a Keeper lease: here over a fake lease table.
    fake_key_locks(monkeypatch)


@pytest.fixture(autouse=True)
def _no_earlier_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # The key held no other status before its retirement.
    stub_status_before(monkeypatch)


def _row(memory_type: str, **over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "ops-org",
        "namespace": "ops-alerts",
        "agent": "sre-agent",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "alert:disk-full:host-7",
        "memory_type": memory_type,
        "value": {"state": "firing"},
        "value_text": '{"state": "firing"}',
        "confidence": 1.0,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=1),
        "expires_at": None,
        "tags": ["alert"],
        "entities": [],
        "run_id": "-",
    }
    base.update(over)
    return MemoryRow(**base)


_DIM0_EMB_ROW = {
    "embedding_model": "",
    "embedding_dim": 0,
    "embedding": [],
    "content_excerpt": '{"state": "firing"}',
    "memory_key_embedding": [],
    "memory_key_embedding_dim": 0,
}
_EMBEDDED_ROW = {
    "embedding_model": "fake-test",
    "embedding_dim": 3,
    "embedding": [0.1, 0.1, 0.1],
    "content_excerpt": "disk full on host-7 (#alert:disk-full:host-7)",
    "memory_key_embedding": [0.2, 0.2, 0.2],
    "memory_key_embedding_dim": 3,
}


class _CH:
    def __init__(self, emb_rows: list[dict[str, Any]], *, fail: tuple[str, ...] = ()) -> None:
        self.emb_rows = emb_rows
        self.fail = fail
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.query_one_result: dict[str, Any] | None = None
        self.sqls: list[str] = []

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return self.query_one_result

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        if "FROM loom.memory_embeddings" in sql:
            return [dict(r) for r in self.emb_rows]
        return []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if table in self.fail:
            raise RuntimeError(f"insert into {table} unavailable")
        self.inserts.append((table, [dict(r) for r in rows], settings or {}))

    async def execute(self, *_: Any, **__: Any) -> None:
        return None

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows, _ in self.inserts if t == table for r in rows]


def _query_one_row(existing: MemoryRow, **over: Any) -> dict[str, Any]:
    base = {
        "memory_key": existing.memory_key,
        "memory_type": existing.memory_type,
        "value": existing.value_text,
        "confidence": existing.confidence,
        "source_trace": "",
        "memory_id": existing.memory_id,
        "entities": list(existing.entities),
        "tags": list(existing.tags),
        "valid_at": None,
        "char_limit": None,
        "pinned": 0,
        "tombstoned_at": existing.tombstoned_at,
        "version": existing.version,
        "status": existing.status,
        "superseded_by": existing.superseded_by,
    }
    base.update(over)
    return base


def _wire(monkeypatch: pytest.MonkeyPatch, existing: MemoryRow, ch: _CH) -> None:
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)

    async def _audit(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("server.observability.audit.emit", _audit)
    monkeypatch.setattr("server.observability.audit.emit_strict", _audit)
    ch.query_one_result = _query_one_row(existing)


def _restore_input() -> MemoryRestoreInput:
    return MemoryRestoreInput(
        org="ops-org",
        namespace="ops-alerts",
        agent="sre-agent",
        memory_key="alert:disk-full:host-7",
    )


# ------------------------------------------------ restore after a delete


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_type", _TYPES)
@pytest.mark.parametrize("emb_row", [_EMBEDDED_ROW, _DIM0_EMB_ROW], ids=["embedded", "dim0"])
async def test_restore_revives_the_embeddings_row_for_every_type(
    monkeypatch: pytest.MonkeyPatch, memory_type: str, emb_row: dict[str, Any]
) -> None:
    existing = _row(memory_type, tombstoned_at=_NOW - timedelta(days=2))
    ch = _CH([emb_row])
    _wire(monkeypatch, existing, ch)
    out = await restore_mod.handle_memory_restore(_restore_input())
    assert out.restored is True
    (mem,) = ch.rows_for("loom.memory")
    assert mem["tombstoned_at"] is None and mem["value"] == '{"state": "firing"}'
    (emb,) = ch.rows_for("loom.memory_embeddings")
    assert set(EMBEDDING_PERSISTED_COLS) <= set(emb)
    assert emb["tombstoned_at"] is None
    assert emb["memory_type"] == memory_type and emb["memory_key"] == existing.memory_key
    # what the table held is what comes back: vectors, or the dim-0 stub
    assert emb["embedding"] == emb_row["embedding"]
    assert emb["embedding_dim"] == emb_row["embedding_dim"]
    assert emb["embedding_model"] == emb_row["embedding_model"]
    assert emb["memory_key_embedding"] == emb_row["memory_key_embedding"]
    # the excerpt is the mirror's, not rebuilt
    assert emb["content_excerpt"] == emb_row["content_excerpt"]
    assert emb["tags"] == ["alert"] and emb["confidence"] == 1.0
    assert emb["status"] == "active"
    # the read that found it is the argMax read delete uses
    assert any("FROM loom.memory_embeddings" in s for s in ch.sqls)


@pytest.mark.asyncio
async def test_restore_announces_its_row_when_the_high_fence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is live once inserted: a failure after it answers an error, and the
    memory.restored event is already in the outbox (a retry finds the key live)."""
    existing = _row("semantic", tombstoned_at=_NOW - timedelta(days=2), sensitivity="high")
    ch = _CH([])
    _wire(monkeypatch, existing, ch)
    announced: list[Any] = []

    async def _announce(changes: Any) -> None:
        announced.extend(changes)

    async def _fence(*_: Any, **__: Any) -> None:
        raise RuntimeError("fence insert failed")

    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    monkeypatch.setattr("server.memory.app.high_fence.fence_high", _fence)
    with pytest.raises(RuntimeError, match="fence"):
        await restore_mod.handle_memory_restore(_restore_input())
    [(event, row)] = announced
    assert event == "memory.restored" and row["tombstoned_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("wrote", "shape"), [(True, "webhook"), (False, "")])
async def test_an_undelete_is_held_to_the_webhook_budget_only_when_it_wrote_outbox_rows(
    monkeypatch: pytest.MonkeyPatch, wrote: bool, shape: str
) -> None:
    existing = _row("semantic", tombstoned_at=_NOW - timedelta(days=2))
    _wire(monkeypatch, existing, _CH([]))

    async def _announce(changes: Any) -> bool:
        return wrote

    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    note_budget_shape("")
    out = await restore_mod.handle_memory_restore(_restore_input())
    assert out.restored is True and current_budget_shape() == shape


@pytest.mark.asyncio
@pytest.mark.parametrize(("landed_late", "shape"), [(False, "undo"), (True, "")])
async def test_an_undo_is_held_to_the_undo_budget_only_when_it_writes_its_record(
    monkeypatch: pytest.MonkeyPatch, landed_late: bool, shape: str
) -> None:
    # an Undo awaits its audit record before its restore version; the retry of one
    # whose restore landed late writes neither
    existing = _row("semantic", status="superseded", superseded_by="other-key")
    _wire(monkeypatch, existing, _CH([]))

    async def _unsupersede(*_: Any, **__: Any) -> int:
        return 1

    async def _late(*_: Any, **__: Any) -> bool:
        return landed_late

    monkeypatch.setattr(memory_repo, "unsupersede_embeddings", _unsupersede)
    monkeypatch.setattr(restore_mod, "_landed_late", _late)
    note_budget_shape("")
    out = await restore_mod.handle_memory_restore(_restore_input())
    assert out.from_status == "superseded" and current_budget_shape() == shape


def test_the_types_cover_both_sides_of_the_old_guard() -> None:
    # a type guard would exclude at least one of the types
    assert {is_searchable_type(t) for t in _TYPES} == {True, False}


@pytest.mark.asyncio
async def test_restore_writes_no_mirror_row_when_the_table_holds_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _row("episodic", tombstoned_at=_NOW - timedelta(days=2))
    ch = _CH([])
    _wire(monkeypatch, existing, ch)
    out = await restore_mod.handle_memory_restore(_restore_input())
    assert out.restored is True
    assert len(ch.rows_for("loom.memory")) == 1
    assert ch.rows_for("loom.memory_embeddings") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_type", _TYPES)
async def test_restore_mirror_stays_fail_open(
    monkeypatch: pytest.MonkeyPatch, memory_type: str
) -> None:
    existing = _row(memory_type, tombstoned_at=_NOW - timedelta(days=2))
    ch = _CH([_DIM0_EMB_ROW], fail=("loom.memory_embeddings",))
    _wire(monkeypatch, existing, ch)
    out = await restore_mod.handle_memory_restore(_restore_input())
    assert out.restored is True  # the memory row is live; the mirror failure is logged
    assert len(ch.rows_for("loom.memory")) == 1


# --------------------------------------------- restore after a supersession


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_type", _TYPES)
async def test_unsupersede_re_emits_the_embeddings_for_every_type(
    monkeypatch: pytest.MonkeyPatch, memory_type: str
) -> None:
    existing = _row(memory_type, status="superseded", superseded_by="other-key")
    ch = _CH([])
    _wire(monkeypatch, existing, ch)
    calls: list[dict[str, Any]] = []

    async def _unsupersede(*args: Any, **kw: Any) -> int:
        calls.append({"args": args, **kw})
        return 1

    monkeypatch.setattr(memory_repo, "unsupersede_embeddings", _unsupersede)
    out = await restore_mod.handle_memory_restore(_restore_input())
    assert out.restored is True and out.from_status == "superseded"
    (mem,) = ch.rows_for("loom.memory")
    assert mem["status"] == "active" and mem["superseded_by"] == ""
    assert len(calls) == 1
    assert calls[0]["args"][:6] == (
        "ops-org",
        "ops-alerts",
        "sre-agent",
        "-",
        "-",
        "alert:disk-full:host-7",
    )


@pytest.mark.asyncio
async def test_unsupersede_failure_leaves_the_memory_row_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _row("episodic", status="superseded", superseded_by="other-key")
    ch = _CH([])
    _wire(monkeypatch, existing, ch)

    async def _boom(*_: Any, **__: Any) -> int:
        raise RuntimeError("mirror unavailable")

    monkeypatch.setattr(memory_repo, "unsupersede_embeddings", _boom)
    out = await restore_mod.handle_memory_restore(_restore_input())
    assert out.restored is True
    assert len(ch.rows_for("loom.memory")) == 1


def test_the_restore_paths_carry_no_type_guard() -> None:
    import inspect

    src = inspect.getsource(restore_mod.handle_memory_restore)
    src += inspect.getsource(restore_mod._restore_superseded)
    assert "is_searchable_type" not in src
    assert '.get("embedding")' not in src  # no non-empty-vector guard either


@pytest.mark.asyncio
async def test_a_restore_inside_an_erasure_records_who_restored_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.auth.context import set_current_auth

    class _ErasureCH(_CH):
        async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
            if "FROM loom.erasure_requests" in sql:
                return [{"erasure_id": "e1", "scope_type": "namespace", "scope_value": "ns"}]
            return await super().query_all(sql, params, **kw)

    existing = _row("semantic", tombstoned_at=_NOW - timedelta(days=2))
    ch = _ErasureCH([])
    _wire(monkeypatch, existing, ch)
    set_current_auth(SimpleNamespace(org="ops-org", subject="alice", is_service_account=False))
    try:
        assert (await restore_mod.handle_memory_restore(_restore_input())).restored
    finally:
        set_current_auth(None)
    (reversal,) = ch.rows_for("loom.erasure_reversals")
    assert reversal["restored_by"] == "human:alice"


@pytest.mark.parametrize("authority", [True, False])
async def test_a_retry_finishing_a_late_undo_re_emits_only_on_clickhouse(
    monkeypatch: pytest.MonkeyPatch, authority: bool
) -> None:
    # under Postgres the commit owes an embedded row's search document to the delivery
    # worker; a copy made outside any lease could land after a redaction's purge
    existing = _row("semantic", status="active")
    _wire(monkeypatch, existing, _CH([]))
    calls: list[str] = []

    async def _unsupersede(*_: Any, **__: Any) -> int:
        calls.append("unsupersede")
        return 1

    async def _late(*_: Any, **__: Any) -> bool:
        return True

    async def _reopen(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("server.core.pg.enabled", lambda: authority)
    monkeypatch.setattr(memory_repo, "unsupersede_embeddings", _unsupersede)
    monkeypatch.setattr(restore_mod, "_landed_late", _late)
    monkeypatch.setattr(restore_mod, "_reopen_facts", _reopen)
    out = await restore_mod._restore_superseded(_restore_input(), _CH([]))
    assert out.restored is True
    assert calls == ([] if authority else ["unsupersede"])


@pytest.mark.parametrize(
    "authority, redacted, mirrored",
    [(False, False, True), (False, True, False), (True, False, False)],
)
async def test_a_live_rows_search_repair_runs_on_clickhouse_for_an_unredacted_head(
    monkeypatch: pytest.MonkeyPatch, authority: bool, redacted: bool, mirrored: bool
) -> None:
    # a redacted head has no search row to bring back; under Postgres the undelete's
    # commit owes it
    existing = _row("semantic", redacted_at=_NOW if redacted else None)
    ch = _CH([])
    _wire(monkeypatch, existing, ch)
    calls: list[int] = []

    async def _unmirrored(*_: Any, **__: Any) -> bool:
        return True

    async def _mirror(*_: Any, version: int) -> None:
        calls.append(version)

    monkeypatch.setattr("server.core.pg.enabled", lambda: authority)
    monkeypatch.setattr(restore_mod, "_undelete_left_unmirrored", _unmirrored)
    monkeypatch.setattr(restore_mod, "_restore_embedding_mirror", _mirror)
    row = {"memory_id": str(existing.memory_id), "version": 100, "memory_type": "semantic"}
    out = await restore_mod._restore_live(_restore_input(), row, ch=ch)
    assert out.restored is mirrored
    assert calls == ([100] if mirrored else [])
