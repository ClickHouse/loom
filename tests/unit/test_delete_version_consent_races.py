"""A deletion refuses only the exact fact its tombstone actually removes."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from server.memory.api.models_crud import MemoryDeleteVersionInput
from server.memory.app import handlers
from server.memory.store import pg_memory, rows
from server.memory.store.delete_checks import checked_state_sha256

_AFTER_CHECKED = handlers._after_checked


def _head(value="first", version=10):
    return rows.MemoryRow(
        org="org",
        namespace="ns",
        agent="agent",
        user_id="-",
        session_id="-",
        memory_key="key",
        memory_type="semantic",
        value=value,
        confidence=1.0,
        version=version,
        created_at=datetime(2026, 1, 1),
        memory_id="00000000-0000-0000-0000-000000000001",
        pinned=False,
        expires_at=None,
    )


def _check():
    return dict(
        org="org",
        namespace="ns",
        agent="agent",
        user_id="-",
        session_id="-",
        memory_key="key",
        checked=10,
        tombstone=20,
        checked_state_sha256=checked_state_sha256(_head()),
    )


@pytest.fixture
def store(monkeypatch):
    import server.memory.store as store

    monkeypatch.setattr(store, "record_delete_check", AsyncMock())
    monkeypatch.setattr(store, "fetch_version_rows", AsyncMock(return_value=[_head()]))
    monkeypatch.setattr(store, "get_by_key", AsyncMock(return_value=_head()))
    monkeypatch.setattr(handlers, "_after_checked", AsyncMock(return_value=([], [])))
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("same_version", [False, True])
async def test_a_known_pre_send_race_returns_changed_without_refusing_the_old_value(
    store, monkeypatch, same_version
):
    current = _head("second", 10 if same_version else 11)
    if same_version:
        store.fetch_version_rows.return_value = [current]
    store.get_by_key.side_effect = [_head(), current]
    monkeypatch.setattr(
        handlers,
        "_write_tombstone_row",
        AsyncMock(side_effect=pg_memory.AdmissionContendedError("head changed")),
    )
    out = await handlers._delete_version_locked(
        MemoryDeleteVersionInput(
            **{
                k: v
                for k, v in _check().items()
                if k not in {"checked", "tombstone", "checked_state_sha256"}
            },
            version=10,
        ),
        rows.Identity(org="org", namespace="ns", agent="agent"),
    )
    assert not out.tombstoned and out.reason == "version_changed"
    assert handlers._write_tombstone_row.await_count == 1
    assert store.record_delete_check.call_args.kwargs == {"done": True}


@pytest.mark.asyncio
async def test_recovery_rechecks_a_second_known_pre_send_race(store, monkeypatch):
    store.get_by_key.side_effect = [_head(), _head("second", 11)]
    monkeypatch.setattr(
        handlers,
        "_write_tombstone_row",
        AsyncMock(side_effect=pg_memory.AdmissionContendedError("head changed")),
    )
    out = await handlers.rerun_delete_check(_check())
    assert out is not None and not out.tombstoned and out.reason == "version_changed"
    assert handlers._write_tombstone_row.await_count == 1
    assert store.record_delete_check.call_args.kwargs == {"done": True}


@pytest.mark.asyncio
async def test_durable_recovery_cannot_substitute_a_same_version_historical_row(store, monkeypatch):
    store.fetch_version_rows.return_value = [_head("second")]
    write = AsyncMock()
    monkeypatch.setattr(handlers, "_write_tombstone_row", write)
    out = await handlers.rerun_delete_check(_check())
    assert out is not None and out.reason == "version_changed"
    write.assert_not_awaited()
    assert store.record_delete_check.call_args.kwargs == {"done": True}


@pytest.mark.asyncio
async def test_committed_tombstone_supplies_the_content_for_owed_retirement(store, monkeypatch):
    tombstone = _head().model_copy(
        update={
            "version": 20,
            "tombstoned_at": datetime(2026, 1, 2),
            "source_trace": "memory.delete_version:10",
        }
    )
    store.fetch_version_rows.return_value = [_head("second"), tombstone]
    finish = AsyncMock(return_value=None)
    monkeypatch.setattr(handlers, "finish_delete_check", finish)
    await handlers.rerun_delete_check(_check())
    removed = finish.call_args.args[0]
    assert removed.value == "first" and removed.version == 10


@pytest.mark.asyncio
async def test_a_retrys_tombstone_supplies_content_when_checked_history_collides(
    store, monkeypatch
):
    tombstone = _head().model_copy(
        update={
            "version": 30,
            "tombstoned_at": datetime(2026, 1, 2),
            "source_trace": "memory.delete_version:10",
        }
    )
    store.fetch_version_rows.return_value = [_head("second")]
    handlers._after_checked.return_value = ([30], [])
    monkeypatch.setattr(handlers, "_version_row", AsyncMock(return_value=tombstone))
    finish = AsyncMock(return_value=None)
    monkeypatch.setattr(handlers, "finish_delete_check", finish)
    await handlers.rerun_delete_check(_check())
    removed = finish.call_args.args[0]
    assert removed.value == "first" and removed.version == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("other_versions", [([30], []), ([], [11])])
async def test_an_unknown_checked_state_can_reconcile_without_recreating_a_tombstone(
    store, monkeypatch, other_versions
):
    handlers._after_checked.return_value = other_versions
    monkeypatch.setattr(
        handlers,
        "_version_row",
        AsyncMock(
            return_value=_head().model_copy(
                update={
                    "version": 30,
                    "tombstoned_at": datetime(2026, 1, 2),
                    "source_trace": "memory.delete_version:10",
                }
            )
        ),
    )
    finish = AsyncMock(return_value=None)
    write = AsyncMock()
    monkeypatch.setattr(handlers, "finish_delete_check", finish)
    monkeypatch.setattr(handlers, "_write_tombstone_row", write)
    check = _check()
    check.pop("checked_state_sha256")
    await handlers.rerun_delete_check(check)
    finish.assert_awaited_once()
    write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_recovery_leaves_a_check_owed_after_bounded_contention_or_an_uncertain_send(
    store, monkeypatch, uncertain
):
    error = RuntimeError("uncertain send") if uncertain else pg_memory.AdmissionContendedError()
    write = AsyncMock(side_effect=error)
    monkeypatch.setattr(handlers, "_write_tombstone_row", write)
    with pytest.raises(type(error)):
        await handlers.rerun_delete_check(_check())
    assert write.await_count == (1 if uncertain else 4)
    store.record_delete_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_without_a_checked_state_cannot_refuse_a_historical_value(
    store, monkeypatch
):
    write = AsyncMock()
    monkeypatch.setattr(handlers, "_write_tombstone_row", write)
    check = _check()
    check.pop("checked_state_sha256")
    out = await handlers.rerun_delete_check(check)
    assert out is not None and out.reason == "version_changed"
    assert store.record_delete_check.call_args.kwargs == {"done": True}
    write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True])
async def test_a_foreign_row_at_the_intended_tombstone_version_is_preserved(
    store, monkeypatch, deleted
):
    foreign = _head("second", 20)
    if deleted:
        foreign = foreign.model_copy(
            update={
                "tombstoned_at": datetime(2026, 1, 2),
                "source_trace": "memory.delete_version:9",
            }
        )
    history = {10: _head(), 20: foreign}

    async def fetch(org, namespace, pairs, **kwargs):
        return [history[version] for key, version in pairs if version in history]

    store.fetch_version_rows.side_effect = fetch
    monkeypatch.setattr(handlers, "_after_checked", _AFTER_CHECKED)
    monkeypatch.setattr(
        store,
        "versions_above",
        AsyncMock(
            return_value={
                ("agent", "-", "-", "key"): [(20, deleted, 9 if deleted else None)],
            }
        ),
    )
    lift, retire = AsyncMock(), AsyncMock()
    monkeypatch.setattr(handlers, "_write_above", lift)
    monkeypatch.setattr(handlers, "_retire_derived", retire)
    out = await handlers.rerun_delete_check(_check())
    assert out is not None and not out.tombstoned
    assert out.found is (not deleted)
    assert out.reason == ("" if deleted else "version_changed")
    lift.assert_awaited_once()
    assert lift.call_args.args[0].value == "second"
    retire.assert_not_awaited()
