"""memory.lock is a write-time invariant.

Service level: which mode refuses which write class, a lapsed lock is no
lock, strictness ordering. Handler level, with the repo stubbed: the
guarded ops raise 423 LOCKED with the structured body, an identical
restatement of a read_only value passes, a WRITER re-lock may only
tighten, clear reports what it left behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from server.core.hashing import sha256_value
from server.memory.api.models_crud import (
    MemoryDeleteInput,
    MemorySetInput,
    MemoryTouchInput,
    MemoryUnpinInput,
    MemoryUpdateByIdInput,
)
from server.memory.api.models_lock import MemoryLockInput, MemoryUnlockInput
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.app import update as update_mod
from server.memory.app import write as write_mod
from server.memory.app.lock import handle_memory_lock, handle_memory_unlock
from server.memory.domain import lock as L
from server.memory.store import MemoryRow
from tests.unit.memory_store import one_holder

_NOW = datetime(2026, 9, 12, 12, 0, 0)


def _row(**overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "deploy_window",
        "memory_type": "semantic",
        "value": "Tue and Thu only",
        "confidence": 0.9,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=3),
        "expires_at": None,
        "content_sha256": sha256_value("Tue and Thu only"),
    }
    base.update(overrides)
    return MemoryRow(**base)


# ---------------------------------------------------------------- service


def test_read_only_refuses_every_class_and_no_delete_only_deletion() -> None:
    ro = _row(lock_mode="read_only", lock_reason="policy")
    nd = _row(lock_mode="no_delete", lock_reason="keep")
    for action in ("value", "expire", "delete"):
        with pytest.raises(HTTPException) as e:
            L.assert_unlocked(ro, action, op="x")  # type: ignore[arg-type]
        assert e.value.status_code == 423
        assert e.value.detail["code"] == "LOCKED"
        assert e.value.detail["refused_action"] == action
    L.assert_unlocked(nd, "value", op="x")
    L.assert_unlocked(nd, "expire", op="x")
    with pytest.raises(HTTPException):
        L.assert_unlocked(nd, "delete", op="x")


def test_a_lapsed_lock_is_no_lock_and_an_open_one_holds() -> None:
    lapsed = _row(lock_mode="read_only", lock_until=_NOW - timedelta(seconds=1))
    assert L.effective_mode(lapsed, now=_NOW) == ""
    L.assert_unlocked(lapsed, "delete", op="x", now=_NOW)
    live = _row(lock_mode="read_only", lock_until=_NOW + timedelta(hours=1))
    assert L.effective_mode(live, now=_NOW) == "read_only"
    open_ended = _row(lock_mode="no_delete")
    assert L.effective_mode(open_ended, now=_NOW) == "no_delete"
    assert L.lock_state(lapsed, now=_NOW)["lock_mode"] == ""
    assert L.lock_state(live, now=_NOW)["lock_until"] == _NOW + timedelta(hours=1)


def test_strictness_ordering() -> None:
    assert L.is_stricter_or_equal("read_only", "no_delete")
    assert L.is_stricter_or_equal("read_only", "read_only")
    assert not L.is_stricter_or_equal("no_delete", "read_only")


def test_refusal_body_names_everything_an_agent_needs() -> None:
    row = _row(
        lock_mode="read_only",
        lock_reason="legal hold",
        locked_by="human:alice",
        lock_until=_NOW + timedelta(days=30),
    )
    exc = L.refusal(row, "delete", op="memory.delete", mode="read_only")
    d = exc.detail
    assert d["memory_key"] == "deploy_window"
    assert d["lock_reason"] == "legal hold"
    assert d["locked_by"] == "human:alice"
    assert d["lock_until"].startswith("2026-10-12T12:00:00")
    assert d["unlock_via"] == L.UNLOCK_VIA
    assert "memory.delete refused" in d["message"]


def test_sql_predicates_agree_with_effective_mode() -> None:
    assert "lock_mode != ''" in L.locked_predicate(
        "m"
    ) and "m.lock_until > now()" in L.locked_predicate("m")
    assert (
        "lock_mode = ''" in L.unlocked_predicate()
        and "lock_until <= now()" in L.unlocked_predicate()
    )


# ---------------------------------------------------------------- handlers


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None


def _stub_repo(monkeypatch: pytest.MonkeyPatch, row: MemoryRow | None) -> _CH:
    from tests.unit.claims_fake import fake_key_locks

    fake_key_locks(monkeypatch)  # an unpin and a pin hold the key's write lock
    ch = _CH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow | None:
        return row

    async def _get_by_id(*_: Any, **__: Any) -> MemoryRow | None:
        return row

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    monkeypatch.setattr("server.memory.store.get_by_id", _get_by_id)
    monkeypatch.setattr("server.memory.store.namespaces_of_id", one_holder)
    return ch


_ID = {"org": "test", "namespace": "personal", "agent": "cursor"}


@pytest.mark.asyncio
async def test_delete_touch_update_refuse_a_read_only_row(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row(lock_mode="read_only", lock_reason="hold")
    ch = _stub_repo(monkeypatch, row)
    with pytest.raises(HTTPException) as e:
        await mem_handlers.handle_memory_delete(
            MemoryDeleteInput(**_ID, memory_key="deploy_window")
        )
    assert e.value.status_code == 423 and e.value.detail["refused_action"] == "delete"
    with pytest.raises(HTTPException) as e:
        await mem_handlers.handle_memory_touch(
            MemoryTouchInput(**_ID, memory_key="deploy_window", ttl_seconds=60)
        )
    assert e.value.detail["refused_action"] == "expire"
    with pytest.raises(HTTPException) as e:
        await update_mod.handle_memory_update_by_id(
            MemoryUpdateByIdInput(org="test", memory_id=row.memory_id, value="Fridays too")
        )
    assert e.value.detail["refused_action"] == "value"
    assert ch.inserts == []  # nothing reached the store


@pytest.mark.asyncio
async def test_no_delete_lets_the_value_change_but_not_the_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(lock_mode="no_delete", lock_reason="keep")
    _stub_repo(monkeypatch, row)
    with pytest.raises(HTTPException) as e:
        await mem_handlers.handle_memory_delete(
            MemoryDeleteInput(**_ID, memory_key="deploy_window")
        )
    assert e.value.detail["lock_mode"] == "no_delete"
    # touch (expiry) is not in no_delete's refused set: it must get past the
    # guard. The stubbed CH returns nothing for the embedding mirror, so we
    # only assert the guard did not fire.
    out = await mem_handlers.handle_memory_touch(
        MemoryTouchInput(**_ID, memory_key="deploy_window", ttl_seconds=60)
    )
    assert out.touched is True


@pytest.mark.asyncio
async def test_set_refuses_a_changed_value_under_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row(lock_mode="read_only", lock_reason="hold")
    _stub_repo(monkeypatch, row)
    with pytest.raises(HTTPException) as e:
        await write_mod.handle_memory_set(
            MemorySetInput(**_ID, memory_key="deploy_window", value="Fridays too", classify=False)
        )
    assert e.value.status_code == 423 and e.value.detail["refused_action"] == "value"
    with pytest.raises(HTTPException) as e:
        await write_mod.handle_memory_set(
            MemorySetInput(
                **_ID,
                memory_key="deploy_window",
                value="Tue and Thu only",
                ttl_seconds=3600,
                classify=False,
            )
        )
    assert e.value.detail["refused_action"] == "expire"


@pytest.mark.asyncio
async def test_set_refuses_a_type_retention_on_a_read_only_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A restatement with new tags stamps the type's retention on a row with no expiry: that
    # is an expiry change, which read_only refuses.
    from types import SimpleNamespace

    from server.memory.app import conflicts

    async def retention(*_: Any) -> Any:
        return SimpleNamespace(retention_days=30)

    monkeypatch.setattr(conflicts, "type_config_for", retention)
    _stub_repo(monkeypatch, _row(lock_mode="read_only", lock_reason="hold"))
    with pytest.raises(HTTPException) as e:
        await write_mod.handle_memory_set(
            MemorySetInput(
                **_ID,
                memory_key="deploy_window",
                value="Tue and Thu only",
                tags=["ops"],
                classify=False,
            )
        )
    assert e.value.status_code == 423 and e.value.detail["refused_action"] == "expire"


@pytest.mark.asyncio
async def test_set_refuses_a_retention_window_on_a_read_only_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same bytes relabelled 'medium' would take the sensitive retention window as a
    # new expiry: the lock refuses that as it refuses a ttl_seconds, as bulk does.
    from server.memory.app import blacklist_guard
    from server.settings import settings

    async def no_match(**_: Any) -> None:
        return None

    monkeypatch.setattr(blacklist_guard, "enforce_on_insert", no_match)
    monkeypatch.setattr(settings, "memory_sensitive_retention_days", 7)
    _stub_repo(monkeypatch, _row(lock_mode="read_only", lock_reason="hold"))
    with pytest.raises(HTTPException) as e:
        await write_mod.handle_memory_set(
            MemorySetInput(
                **_ID,
                memory_key="deploy_window",
                value="Tue and Thu only",
                sensitivity="medium",
                classify=False,
            )
        )
    assert e.value.status_code == 423 and e.value.detail["refused_action"] == "expire"


@pytest.mark.asyncio
async def test_lock_writes_a_version_row_and_only_tightens(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _stub_repo(monkeypatch, _row())
    out = await handle_memory_lock(
        MemoryLockInput(
            **_ID,
            memory_key="deploy_window",
            mode="no_delete",
            reason="keep",
            locked_by="human:dev",
        )
    )
    assert out.locked and out.lock_mode == "no_delete" and out.previous_mode == ""
    table, rows = ch.inserts[-1]
    assert table == "loom.memory"
    r = rows[0]
    assert r["lock_mode"] == "no_delete" and r["lock_reason"] == "keep"
    assert r["locked_by"] == "human:dev" and r["locked_at"] is not None and r["lock_until"] is None
    assert r["version"] > 100 and r["value"] is not None  # every live column carried forward

    # tighten: allowed
    _stub_repo(monkeypatch, _row(lock_mode="no_delete", lock_reason="keep"))
    out = await handle_memory_lock(
        MemoryLockInput(**_ID, memory_key="deploy_window", mode="read_only", reason="freeze")
    )
    assert out.locked and out.previous_mode == "no_delete"

    # loosen the mode: refused
    _stub_repo(monkeypatch, _row(lock_mode="read_only", lock_reason="freeze"))
    with pytest.raises(HTTPException) as e:
        await handle_memory_lock(
            MemoryLockInput(**_ID, memory_key="deploy_window", mode="no_delete", reason="relax")
        )
    assert e.value.status_code == 423 and e.value.detail["refused_action"] == "loosen"

    # shorten an open-ended lock: refused; extend a bounded one: allowed
    with pytest.raises(HTTPException):
        await handle_memory_lock(
            MemoryLockInput(
                **_ID, memory_key="deploy_window", mode="read_only", reason="r", ttl_seconds=60
            )
        )
    _stub_repo(monkeypatch, _row(lock_mode="read_only", lock_until=_NOW + timedelta(hours=1)))
    out = await handle_memory_lock(
        MemoryLockInput(
            **_ID,
            memory_key="deploy_window",
            mode="read_only",
            reason="r",
            until=datetime(2030, 1, 1),
        )
    )
    assert out.locked and out.lock_until == datetime(2030, 1, 1)


@pytest.mark.asyncio
async def test_lock_refuses_a_past_until_and_misses_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_repo(monkeypatch, _row())
    with pytest.raises(HTTPException) as e:
        await handle_memory_lock(
            MemoryLockInput(
                **_ID, memory_key="deploy_window", reason="r", until=datetime(2001, 1, 1)
            )
        )
    assert e.value.status_code == 422
    _stub_repo(monkeypatch, None)
    out = await handle_memory_lock(MemoryLockInput(**_ID, memory_key="nope", reason="r"))
    assert out.locked is False


@pytest.mark.asyncio
async def test_unlock_clears_every_lock_column(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _stub_repo(
        monkeypatch,
        _row(
            lock_mode="read_only",
            lock_reason="hold",
            locked_by="human:x",
            lock_until=datetime(2030, 1, 1),
            locked_at=_NOW,
        ),
    )
    out = await handle_memory_unlock(MemoryUnlockInput(**_ID, memory_key="deploy_window"))
    assert out.unlocked and out.previous_mode == "read_only"
    r = ch.inserts[-1][1][0]
    assert r["lock_mode"] == "" and r["lock_reason"] == "" and r["locked_by"] == ""
    assert r["lock_until"] is None and r["locked_at"] is None
    # nothing locked: no version row
    ch = _stub_repo(monkeypatch, _row())
    out = await handle_memory_unlock(MemoryUnlockInput(**_ID, memory_key="deploy_window"))
    assert out.unlocked is False and ch.inserts == []


async def test_unpin_clears_only_the_pin_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _stub_repo(monkeypatch, _row(pinned=True, tags=["keep-me"]))
    out = await mem_handlers.handle_memory_unpin(
        MemoryUnpinInput(**_ID, memory_key="deploy_window")
    )
    assert out.unpinned and out.reason == ""
    row = ch.inserts[-1][1][0]
    assert row["pinned"] == 0 and row["tags"] == ["keep-me"]

    ch = _stub_repo(monkeypatch, _row(pinned=False))
    out = await mem_handlers.handle_memory_unpin(
        MemoryUnpinInput(**_ID, memory_key="deploy_window")
    )
    assert out.unpinned is False and out.reason == "not_pinned" and ch.inserts == []


def test_lock_columns_ride_every_version_row() -> None:
    from server.memory.store import build_memory_version_row

    src = _row(lock_mode="no_delete", lock_reason="keep", locked_by="human:x", locked_at=_NOW)
    row = build_memory_version_row(src, version=101, value="changed")
    assert row["lock_mode"] == "no_delete" and row["lock_reason"] == "keep"
    assert row["locked_by"] == "human:x" and row["locked_at"] == _NOW and row["lock_until"] is None


def test_lock_ops_are_registered_with_the_asymmetric_roles() -> None:
    from server.operations.base import Role
    from server.operations.registry import ALL_OPERATIONS

    by_name = {o.name: o for o in ALL_OPERATIONS}
    assert by_name["memory.lock"].required_role == Role.WRITER
    assert by_name["memory.unlock"].required_role == Role.ADMIN
    assert by_name["memory.lock"].audit and by_name["memory.unlock"].audit


def test_the_read_projection_carries_every_persisted_column() -> None:
    """A column build_memory_version_row carries forward but the FINAL
    projection does not read is zeroed on the next version row. The two lists must agree."""
    import re

    from server.memory import store as R

    names = set(re.findall(r"[a-z_0-9]+", R._MEMORY_FINAL_PROJECTION))
    missing = [c for c in R.MEMORY_PERSISTED_COLS if c not in names]
    assert missing == [], missing


@pytest.mark.asyncio
async def test_a_lock_or_delete_lands_above_a_head_from_a_clock_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A head written on a replica whose clock is a second ahead outranks a version drawn
    here: the lock, unlock and delete write above it, and answer the version written."""
    from server.memory.store.rows import next_version

    ahead = next_version() + 10**9
    ch = _stub_repo(monkeypatch, _row(version=ahead))
    locked = await handle_memory_lock(
        MemoryLockInput(**_ID, memory_key="deploy_window", mode="no_delete", reason="keep")
    )
    assert locked.version == ahead + 2 == ch.inserts[-1][1][0]["version"]

    ch = _stub_repo(monkeypatch, _row(version=ahead, lock_mode="no_delete", lock_reason="k"))
    unlocked = await handle_memory_unlock(MemoryUnlockInput(**_ID, memory_key="deploy_window"))
    assert unlocked.version == ahead + 2 == ch.inserts[-1][1][0]["version"]

    ch = _stub_repo(monkeypatch, _row(version=ahead))
    deleted = await mem_handlers.handle_memory_delete(
        MemoryDeleteInput(**_ID, memory_key="deploy_window")
    )
    memory_rows = [rows for table, rows in ch.inserts if table == "loom.memory"]
    assert deleted.version == ahead + 2 == memory_rows[0][0]["version"]
