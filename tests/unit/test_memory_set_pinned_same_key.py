"""A pinned row is never evicted by a memory.set under its own key.

The conflict gate's peer search excludes the writer's own key (supersession is
the same-key rule), so without its own check a `memory.set` of a new value
under a pinned key would write a new version with `pinned=0` and answer 200
`clear`. The point path refuses it the way a close write against a pinned
peer is refused, and the way the bulk path skips a pinned target.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

import server.memory.app.write as _mw
from server.core.hashing import sha256_value
from server.memory.api.models_crud import MemorySetInput
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.app import write as write_mod
from server.memory.store import MemoryRow

_NOW = datetime.now(UTC).replace(tzinfo=None)
_ID = "51c3cdf5-7ea2-4b79-9d34-a6b5d7a9a2b2"
_KEY = "test/loomcheck/pinned"
_OLD = "Release branches are cut on Tuesdays"


def _row(**overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": _ID,
        "org": "test",
        "namespace": "personal",
        "agent": "claude-desktop",
        "user_id": "-",
        "session_id": "-",
        "memory_key": _KEY,
        "memory_type": "semantic",
        "value": _OLD,
        "confidence": 1.0,
        "version": 100,
        "pinned": True,
        "created_at": _NOW - timedelta(days=1),
        "expires_at": None,
        "entities": [],
        "content_sha256": sha256_value(_OLD),
    }
    base.update(overrides)
    return MemoryRow(**base)


def _set(value: str, **kw: Any) -> MemorySetInput:
    return MemorySetInput(
        org="test",
        namespace="personal",
        agent="claude-desktop",
        memory_key=_KEY,
        value=value,
        classify=False,
        **kw,
    )


# ---- the guard ----------------------------------------------------------------


def test_a_changed_value_over_a_pinned_row_is_a_409() -> None:
    with pytest.raises(HTTPException) as e:
        _mw._guard_existing_row(_row(), _set("Release branches are cut on Fridays"), "")
    assert e.value.status_code == 409
    assert e.value.detail["code"] == "PINNED_CONFLICT"
    assert e.value.detail["pinned_blocker_id"] == _ID
    assert "memory.update" in e.value.detail["message"]


def test_a_dry_run_answers_rejected_instead_of_raising() -> None:
    restated, out = _mw._guard_existing_row(
        _row(), _set("Release branches are cut on Fridays", dry_run=True), ""
    )
    assert restated is False
    assert out is not None
    assert out.conflict_verdict == "rejected" and out.dry_run and out.noop
    assert out.version == 0 and out.memory_id == _ID


def test_a_restatement_keeps_the_pin() -> None:
    inp = _set(_OLD)
    restated, out = _mw._guard_existing_row(_row(), inp, "")
    assert restated is True and out is None
    assert inp.pinned is True, "a restated pinned row is written pinned again"


def test_a_pinning_write_replaces_a_pinned_value() -> None:
    inp = _set("Release branches are cut on Fridays", pinned=True)
    assert _mw._guard_existing_row(_row(), inp, "") == (False, None)


def test_an_unpinned_row_takes_the_new_value() -> None:
    inp = _set("Release branches are cut on Fridays")
    assert _mw._guard_existing_row(_row(pinned=False), inp, "") == (False, None)
    assert inp.pinned is False


def test_no_live_row_is_a_first_write() -> None:
    assert _mw._guard_existing_row(None, _set("x"), "") == (False, None)


def test_the_lock_is_still_met_before_the_pin() -> None:
    locked = _row(lock_mode="read_only", lock_reason="freeze", locked_by="human:a")
    with pytest.raises(HTTPException) as e:
        _mw._guard_existing_row(locked, _set("Release branches are cut on Fridays"), "")
    assert e.value.status_code == 423


# ---- through memory.set ---------------------------------------------------------


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


def _wire(monkeypatch: pytest.MonkeyPatch, existing: MemoryRow | None) -> _CH:
    ch = _CH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch)

    async def _existing(*_: Any, **__: Any) -> MemoryRow | None:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _existing)
    monkeypatch.setattr("server.memory.store.get_by_id", _existing)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    import server.memory.app.blacklist_guard as _bl
    import server.memory.app.conflicts as _cf

    monkeypatch.setattr(_bl, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(_cf, "get_client", lambda: ch, raising=False)
    return ch


@pytest.mark.asyncio
async def test_memory_set_over_a_pinned_key_writes_nothing(monkeypatch) -> None:
    ch = _wire(monkeypatch, _row())
    with pytest.raises(HTTPException) as e:
        await write_mod.handle_memory_set(_set("Release branches are cut on Fridays"))
    assert e.value.status_code == 409 and e.value.detail["code"] == "PINNED_CONFLICT"
    assert not [t for t, _ in ch.inserts if t == "loom.memory"], "the pinned row stays"


@pytest.mark.asyncio
async def test_memory_pin_over_a_pinned_key_writes_the_new_value(monkeypatch) -> None:
    ch = _wire(monkeypatch, _row())
    out = await write_mod.handle_memory_set(
        _set("Release branches are cut on Fridays", pinned=True)
    )
    assert out.pinned is True
    rows = [r for t, rs in ch.inserts if t == "loom.memory" for r in rs]
    assert rows and rows[0]["pinned"] == 1


@pytest.mark.asyncio
async def test_memory_set_goes_above_a_head_written_ahead_of_this_clock(monkeypatch) -> None:
    # under Postgres the commit keeps the newer head: a value below it would be committed
    # and never served
    from server.memory.store.rows import next_version

    ahead = next_version() + 15 * 60 * 10**9  # a replica whose clock runs 15 min ahead
    ch = _wire(monkeypatch, _row(pinned=False, version=ahead))
    out = await write_mod.handle_memory_set(_set("Release branches are cut on Fridays"))
    rows = [r for t, rs in ch.inserts if t == "loom.memory" for r in rs]
    assert out.version == rows[0]["version"] == ahead + 2
