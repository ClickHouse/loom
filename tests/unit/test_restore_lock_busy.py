"""An Undo that cannot take the key's write lock answers in its own words, and stays
retryable: 409 while a conditional write holds the key, 503 while Keeper is down."""

from contextlib import asynccontextmanager

import pytest

from server.core import leases
from server.core.errors import PreconditionFailedError
from server.memory.api.models_crud import MemoryRestoreInput
from server.memory.app import restore
from server.memory.store import key_write_lock

_UNDO = MemoryRestoreInput(
    org="o", namespace="ns", agent="a", memory_key="k", if_superseded_by="winner"
)


def _lock_failing_with(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    @asynccontextmanager
    async def hold(*_args):
        raise error
        yield

    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", hold)


async def test_an_undo_meeting_a_conditional_write_says_to_retry_the_undo(monkeypatch) -> None:
    _lock_failing_with(monkeypatch, key_write_lock._busy())
    with pytest.raises(PreconditionFailedError) as e:
        await restore._restore_superseded(_UNDO, ch=None)
    assert e.value.status_code == 409
    assert "conditional" not in str(e.value) and "retry the undo" in str(e.value).lower()


async def test_an_undo_with_keeper_down_says_to_retry_the_undo(monkeypatch) -> None:
    down = leases.LeaseUnavailable("the conditional write's key lock could not be taken")
    _lock_failing_with(monkeypatch, down)
    with pytest.raises(leases.LeaseUnavailable) as e:
        await restore._restore_superseded(_UNDO, ch=None)
    assert e.value.status_code == 503
    assert "conditional" not in str(e.value) and "retry the undo" in str(e.value).lower()


async def test_an_error_inside_the_undo_is_not_reworded(monkeypatch) -> None:
    @asynccontextmanager
    async def hold(*_args):
        yield

    async def read(_input):
        raise PreconditionFailedError("from the body")

    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", hold)
    monkeypatch.setattr(restore, "_read_restorable", read)
    with pytest.raises(PreconditionFailedError, match="from the body"):
        await restore._restore_superseded(_UNDO, ch=None)


async def test_the_undo_of_a_delete_takes_the_keys_write_lock(monkeypatch) -> None:
    from datetime import UTC, datetime

    class CH:
        async def query_one(self, sql: str, params: object = None) -> dict:
            # The key's head: deleted a minute ago.
            return {
                "memory_key": "k",
                "memory_type": "semantic",
                "memory_id": "m1",
                "tombstoned_at": datetime.now(UTC),
                "version": 3,
                "status": "active",
                "superseded_by": "",
            }

    monkeypatch.setattr(restore, "get_client", lambda: CH())
    _lock_failing_with(monkeypatch, key_write_lock._busy())
    undo = MemoryRestoreInput(org="o", namespace="ns", agent="a", memory_key="k")
    with pytest.raises(PreconditionFailedError) as e:
        await restore.handle_memory_restore(undo)
    assert e.value.status_code == 409 and "retry the restore" in str(e.value).lower()


async def test_a_deleted_key_whose_text_was_redacted_is_not_restored(monkeypatch) -> None:
    """Deleted, then redacted: the tombstone holds only the placeholder, so a restore
    answers `redacted` and writes nothing."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import server.memory.store as memory_repo

    class CH:
        async def query_one(self, sql: str, params: object = None) -> dict:
            return {"memory_id": "m1", "tombstoned_at": datetime.now(UTC), "version": 3}

    @asynccontextmanager
    async def hold(*_args):
        yield

    async def tombstone(*_a, **_k):
        return SimpleNamespace(
            memory_id="m1", version=3, tombstoned_at=datetime.now(UTC), redacted_at=datetime.now()
        )

    async def never(*_a, **_k):
        raise AssertionError("nothing is written for a redacted tombstone")

    monkeypatch.setattr(restore, "get_client", lambda: CH())
    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", hold)
    monkeypatch.setattr(memory_repo, "get_by_key", tombstone)
    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", never)
    monkeypatch.setattr(restore.webhooks, "announce", never)
    out = await restore.handle_memory_restore(
        MemoryRestoreInput(org="o", namespace="ns", agent="a", memory_key="k")
    )
    assert (out.restored, out.redacted, out.version) == (False, True, 3)
