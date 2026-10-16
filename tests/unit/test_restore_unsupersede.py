"""memory.restore reverses an automatic supersession: status back to active,
retirement stamp cleared, embeddings re-emitted, audit event
memory.unsupersede."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.memory.store import MemoryRow


@pytest.fixture(autouse=True)
def _key_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Undo holds the key's write lock, a Keeper lease: here over a fake lease table.
    from server.memory.store import key_write_lock as kl
    from tests.unit.claims_fake import FakeLockTable

    locks = kl.KeyWriteLocks(FakeLockTable(), wait_s=0.3)  # type: ignore[arg-type]
    monkeypatch.setattr(kl, "key_write_locks", locks)

# --- memory.restore on a superseded row -------------------------------------


class _CH:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row, self.inserts, self.query_all_calls = row, [], 0

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any]:
        return self.row

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        self.query_all_calls += 1
        return []

    async def insert(self, table: str, rows: list[dict[str, Any]], **__: Any) -> None:
        self.inserts.append((table, rows))


def _superseded_row() -> MemoryRow:
    return MemoryRow(
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="s1",
        memory_key="k",
        memory_type="semantic",
        value="v",
        confidence=0.9,
        version=100,
        created_at=datetime(2026, 9, 1),
        status="superseded",
        superseded_by="keeper-id",
        invalid_at=datetime(2026, 9, 10),
        memory_id="mid",
        pinned=False,
        expires_at=None,
    )


@pytest.mark.asyncio
async def test_restore_reverses_an_automatic_supersession_and_writes_the_ledger_row(monkeypatch):
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import handlers as h
    from server.memory.app import restore as _restore_mod
    from server.memory.app import restore as restore_mod
    from server.memory.app import stack as _stack_mod

    ch = _CH(
        {
            "memory_key": "k",
            "memory_type": "semantic",
            "value": "v",
            "confidence": 0.9,
            "source_trace": "",
            "memory_id": "mid",
            "entities": [],
            "tags": [],
            "valid_at": None,
            "char_limit": None,
            "pinned": 0,
            "tombstoned_at": None,
            "version": 100,
            "status": "superseded",
            "superseded_by": "keeper-id",
        }
    )
    monkeypatch.setattr(h, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", h.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", h.get_client)
    existing = _superseded_row()

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    emitted: list[tuple[str, dict[str, Any]]] = []

    async def _emit(event_type: str, **kw: Any) -> str:
        emitted.append((event_type, kw))
        return "hash"

    unsup: list[str] = []

    async def _unsupersede(*a: Any, **k: Any) -> int:
        unsup.append(a[5])
        return 1

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    monkeypatch.setattr("server.memory.store.unsupersede_embeddings", _unsupersede)
    monkeypatch.setattr("server.observability.audit.emit", _emit)
    from types import SimpleNamespace

    from server.auth.context import set_current_auth

    set_current_auth(SimpleNamespace(org="o", subject="alice", is_service_account=False))
    try:
        out = await restore_mod.handle_memory_restore(
            MemoryRestoreInput(org="o", namespace="n", agent="a", session_id="s1", memory_key="k")
        )
    finally:
        set_current_auth(None)
    assert out.restored is True and out.from_status == "superseded"
    # The ledger row names who undid the retirement, from the token.
    assert emitted[0][1]["actor"] == "human:alice"
    table, rows = ch.inserts[0]
    assert table == "loom.memory"
    assert (
        rows[0]["status"] == "active"
        and rows[0]["superseded_by"] == ""
        and rows[0]["invalid_at"] is None
    )
    assert rows[0]["version"] > 100, "a newer version wins under FINAL"
    assert unsup == ["k"], "embeddings re-emitted active"
    assert emitted and emitted[0][0] == "memory.unsupersede"
    subj = emitted[0][1]["subject"]
    assert subj["superseded_by"] == "keeper-id" and "retired_by" not in subj


@pytest.mark.parametrize("deleted_since", [True, False])
async def test_an_undo_after_a_later_delete_leaves_the_delete(monkeypatch, deleted_since):
    # The first read still shows the retirement; the read-your-writes head shows the
    # delete that landed since, or no row at all. The delete stands, and the answer
    # comes from that head.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    ch = _CH({"memory_id": "stale", "version": 100, "status": "superseded", "tombstoned_at": None})
    deleted = _superseded_row().model_copy(
        update={"version": 101, "tombstoned_at": datetime(2026, 9, 11)}
    )

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow | None:
        return deleted if deleted_since else None

    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    out = await restore_mod.handle_memory_restore(
        MemoryRestoreInput(
            org="o",
            namespace="n",
            agent="a",
            session_id="s1",
            memory_key="k",
            if_superseded_by="keeper-id",
        )
    )
    assert out.restored is False and ch.inserts == []
    if deleted_since:
        assert (out.memory_id, out.version) == ("mid", 101)
    else:
        assert out.memory_id is None


async def test_a_guarded_restore_never_undoes_a_delete(monkeypatch):
    # The inspector showed the row retired; another client deleted it since.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    yesterday = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    deleted = _superseded_row().model_copy(update={"version": 101, "tombstoned_at": yesterday})
    ch = _CH(
        {
            "memory_id": "mid",
            "version": 101,
            "status": "superseded",
            "tombstoned_at": deleted.tombstoned_at,
        }
    )

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return deleted

    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    out = await restore_mod.handle_memory_restore(
        MemoryRestoreInput(
            org="o",
            namespace="n",
            agent="a",
            session_id="s1",
            memory_key="k",
            only_if_superseded=True,
        )
    )
    assert out.restored is False and out.from_status == "" and ch.inserts == []
