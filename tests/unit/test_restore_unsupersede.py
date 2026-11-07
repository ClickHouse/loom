"""memory.restore reverses an automatic supersession: status back to active,
retirement stamp cleared, embeddings re-emitted, audit event
memory.unsupersede."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.store import MemoryRow
from tests.unit.claims_fake import fake_key_locks
from tests.unit.memory_store import stub_status_before


@pytest.fixture(autouse=True)
def _key_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Undo holds the key's write lock, a Keeper lease: here over a fake lease table.
    fake_key_locks(monkeypatch)


@pytest.fixture(autouse=True)
def earlier_status_reads(monkeypatch: pytest.MonkeyPatch) -> list[MemoryRow]:
    # The key held no other status before its retirement; a test that needs one says so.
    return stub_status_before(monkeypatch)


# --- memory.restore on a superseded row -------------------------------------


class _CH:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row, self.inserts = row, []

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any]:
        return self.row

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
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
async def test_restore_reverses_an_automatic_supersession_and_writes_the_ledger_row(
    monkeypatch, earlier_status_reads
):
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

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    monkeypatch.setattr(memory_repo, "unsupersede_embeddings", _unsupersede)
    monkeypatch.setattr("server.observability.audit.emit", _emit)
    monkeypatch.setattr("server.observability.audit.emit_strict", _emit)
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
    # It asks about the retired head it read, not the version it writes.
    assert earlier_status_reads == [existing]
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
    assert subj["version"] == out.version, "the resolver reads the restore's version"


@pytest.mark.asyncio
async def test_a_retried_undo_that_landed_late_answers_restored_when_its_search_document_fails(
    monkeypatch, caplog
):
    """On ClickHouse the retry finishes an Undo whose restore landed after its 503: a
    search document's failure is logged by key and the restore still answers as written."""
    from server.core import pg
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    landed = _superseded_row().model_copy(update={"status": "active", "version": 107})
    reopened: list[int] = []

    async def _read(_input: Any) -> MemoryRow:
        return landed

    async def _landed_late(_input: Any, _head: Any) -> bool:
        return True

    async def _document_down(*_: Any, **__: Any) -> None:
        raise RuntimeError("clickhouse down")

    async def _reopen(_input: Any, _existing: Any, version: int, **_: Any) -> None:
        reopened.append(version)

    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(restore_mod, "_read_restorable", _read)
    monkeypatch.setattr(restore_mod, "_landed_late", _landed_late)
    monkeypatch.setattr(restore_mod, "_unsupersede", _document_down)
    monkeypatch.setattr(restore_mod, "_reopen_facts", _reopen)
    out = await restore_mod._restore_superseded(
        MemoryRestoreInput(org="o", namespace="n", agent="a", session_id="s1", memory_key="k"),
        None,
    )
    assert (out.restored, out.version, reopened) == (True, 107, [107])
    assert "search document failed for k" in caplog.text


@pytest.mark.asyncio
async def test_an_undo_whose_record_fails_restores_nothing(monkeypatch):
    # The resolver honours an Undo only by its record: an Undo it could not see would
    # be judged again, so a failed record fails the Undo before the restore is written.
    from server.core.errors import BackendUnavailableError
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    ch = _CH({"status": "superseded", "superseded_by": "keeper-id", "tombstoned_at": None})
    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return _superseded_row()

    async def _boom(*_: Any, **__: Any) -> str:
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    monkeypatch.setattr("server.observability.audit.emit_strict", _boom)
    with pytest.raises(BackendUnavailableError) as raised:
        await restore_mod.handle_memory_restore(
            MemoryRestoreInput(org="o", namespace="n", agent="a", session_id="s1", memory_key="k")
        )
    assert raised.value.status_code == 503 and "nothing was restored" in str(raised.value)
    assert ch.inserts == [], "nothing restored"


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
    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
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
    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
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


_PAST = datetime(2026, 1, 1)
_FUTURE = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)


@pytest.mark.parametrize(
    ("keeper", "status"),
    [
        ({"status": "active"}, "duplicate"),
        ({"status": "conflicted"}, "duplicate"),
        # Marked high since: still served to those who may read it, so the copy stays hidden.
        ({"status": "active", "sensitivity": "high"}, "duplicate"),
        ({"status": "active", "expires_at": _FUTURE}, "duplicate"),
        ({"status": "active", "valid_to": _FUTURE}, "duplicate"),
        (None, "active"),
        # Reads serve none of these, so the restored row must not hide behind them.
        ({"status": "proposed"}, "active"),
        ({"status": "duplicate"}, "active"),
        ({"status": "active", "expires_at": _PAST}, "active"),
        ({"status": "active", "valid_to": _PAST}, "active"),
    ],
)
async def test_undoing_a_retired_duplicate_keeps_it_a_duplicate(monkeypatch, keeper, status):
    # The same-key judge can retire a duplicate row. Undone while the row it
    # duplicates is still served, it stays hidden behind that row, on both tables.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    stub_status_before(monkeypatch, "duplicate")
    ch = _CH({"memory_id": "mid", "status": "superseded", "tombstoned_at": None})
    existing = _superseded_row().model_copy(update={"duplicate_of": "keeper-of-the-fact"})
    original = None
    if keeper is not None:
        original = _superseded_row().model_copy(
            update={"memory_id": "keeper-of-the-fact", "superseded_by": "", **keeper}
        )

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    async def _get_by_id(memory_id: str, **_: Any) -> MemoryRow | None:
        return original if memory_id == "keeper-of-the-fact" else None

    unsup: list[str] = []

    async def _unsupersede(*a: Any, status: str, **k: Any) -> int:
        unsup.append(status)
        return 1

    async def _emit(*_: Any, **__: Any) -> str:
        return "hash"

    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    monkeypatch.setattr(memory_repo, "get_by_id", _get_by_id)
    monkeypatch.setattr(memory_repo, "unsupersede_embeddings", _unsupersede)
    monkeypatch.setattr("server.observability.audit.emit", _emit)
    monkeypatch.setattr("server.observability.audit.emit_strict", _emit)
    out = await restore_mod.handle_memory_restore(
        MemoryRestoreInput(org="o", namespace="n", agent="a", session_id="s1", memory_key="k")
    )
    assert out.restored is True
    table, rows = ch.inserts[0]
    assert table == "loom.memory" and rows[0]["status"] == status
    assert unsup == [status]


def test_a_version_becomes_its_time_to_the_microsecond() -> None:
    from datetime import datetime

    from server.memory.store.rows import version_time

    # A float of these ns rounds the microsecond up; integer arithmetic does not.
    assert version_time(1_792_383_601_496_448_999) == datetime(2026, 10, 19, 4, 20, 1, 496448)
