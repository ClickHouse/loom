"""Metadata copies must not restore a value replaced by another replica."""

from __future__ import annotations

from typing import Any

import pytest

from server.core import lease_writes
from server.memory import store as memory_repo
from server.memory.api.models_crud import MemoryVerifyInput
from server.memory.api.models_lock import MemoryLockInput, MemoryUnlockInput
from server.memory.app import lock, verify
from server.memory.store import rows
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.pg_memory import AdmissionContendedError
from tests.unit.test_memory_verify import _row


class Replica:
    """Two clients sharing the same versioned table, without replica lag."""

    def __init__(self, table: list[dict[str, Any]]) -> None:
        self.table = table

    @property
    def head(self) -> dict[str, Any]:
        return max(self.table, key=lambda r: r["version"])

    async def insert(self, table: str, batch: list[dict[str, Any]], **kw: Any) -> None:
        assert table == "loom.memory"
        self.table.extend(batch)

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        assert "loom.memory" in sql
        assert "select_sequential_consistency = 1" in sql
        return [self.head]


@pytest.fixture
def fenced_store(monkeypatch: pytest.MonkeyPatch, memory_insert_keeper):
    """The existing Keeper fake holds names shared by both clients."""
    monkeypatch.setattr(rows, "next_version", lambda: 300)
    monkeypatch.setattr(verify, "_schedule_mirror", lambda *args, **kw: None)
    return memory_insert_keeper.keys


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["lock", "unlock", "verify", "revoke"])
@pytest.mark.parametrize("moved", [False, True])
async def test_metadata_copy_checks_the_head_under_shared_writer_fence(
    monkeypatch: pytest.MonkeyPatch, fenced_store: set[str], operation: str, moved: bool
) -> None:
    source = _row(lock_mode="no_delete", verified_by="human:reviewer", verified_sha256="old")
    table = [memory_repo.build_memory_version_row(source)]
    reader, writer = Replica(table), Replica(table)
    update = memory_repo.build_memory_version_row(source, value="rejected", version=200)

    async def read(*args: Any, **kw: Any):
        # The read has captured source; the other replica's update returns before
        # the metadata writer builds its copy.
        if moved:
            await insert_memory_versions([update], ch=writer)
        return source

    monkeypatch.setattr(lock, "_head", read)
    monkeypatch.setattr(verify, "_resolve_row", read)
    monkeypatch.setattr(lock, "get_client", lambda: reader)
    monkeypatch.setattr(verify, "get_client", lambda: reader)
    identity = dict(
        org=source.org, namespace=source.namespace, agent=source.agent, memory_key=source.memory_key
    )
    if operation == "lock":
        call = lock.handle_memory_lock(MemoryLockInput(**identity, reason="hold"))
    elif operation == "unlock":
        call = lock.handle_memory_unlock(MemoryUnlockInput(**identity))
    else:
        call = verify.handle_memory_verify(
            MemoryVerifyInput(
                org=source.org,
                memory_id=source.memory_id,
                actor="human:reviewer",
                revoke=operation == "revoke",
            )
        )
    if moved:
        with pytest.raises(AdmissionContendedError):
            await call
        assert reader.head == update
        assert len(table) == 2  # no stale copy even in history
        assert reader.head["value"] == "rejected"
    else:
        await call
        assert len(table) == 2
        expected = dict(
            table[0],
            **{
                k: reader.head[k]
                for k in (
                    "version",
                    "updated_at",
                    "lock_mode",
                    "lock_reason",
                    "lock_until",
                    "locked_by",
                    "locked_at",
                    "verified_by",
                    "verified_at",
                    "verified_sha256",
                    "content_sha256",
                )
            },
        )
        assert reader.head == expected
    assert not fenced_store


@pytest.mark.asyncio
async def test_value_insert_cannot_enter_the_exact_check_insert_window(
    monkeypatch: pytest.MonkeyPatch, memory_insert_keeper: Any
) -> None:
    import asyncio

    from server.memory.store import write_fence

    source = _row()
    table = [memory_repo.build_memory_version_row(source)]
    first, second = Replica(table), Replica(table)
    copy = memory_repo.build_memory_version_row(source, version=300, lock_mode="read_only")
    update = memory_repo.build_memory_version_row(source, value="rejected", version=200)
    check = write_fence.clickhouse_head_versions

    async def competing_write(keys: Any, **kw: Any):
        assert memory_insert_keeper.keys
        # A separate task and client model another serving process. Both must
        # take the same Keeper name even for a plain value insert.
        with pytest.raises(lease_writes.FenceUnavailableError):
            await asyncio.create_task(insert_memory_versions([update], ch=second))
        return await check(keys, **kw)

    monkeypatch.setattr(write_fence, "clickhouse_head_versions", competing_write)
    await insert_memory_versions([copy], ch=first, expected_heads={rows.identity_key(source): 100})
    assert first.head == copy
    assert len(table) == 2
    assert not memory_insert_keeper.keys


@pytest.mark.asyncio
async def test_uncertain_insert_keeps_every_key_fenced(memory_insert_keeper: Any) -> None:
    from server.memory.store import write_fence

    source = _row()
    batch = [memory_repo.build_memory_version_row(source, memory_key=key) for key in ("a", "b")]
    table: list[dict[str, Any]] = []

    class LostAnswer(Replica):
        async def insert(self, *args: Any, **kw: Any) -> None:
            await super().insert(*args, **kw)
            raise TimeoutError("the insert may have landed")

    with pytest.raises(TimeoutError):
        await insert_memory_versions(batch, ch=LostAnswer(table))
    assert set(memory_insert_keeper.keys) == {
        write_fence.lease_name(rows.identity_key(row)) for row in batch
    }
    with pytest.raises(lease_writes.FenceUnavailableError):
        await insert_memory_versions(batch[:1], ch=Replica(table))
    assert len(table) == 2


@pytest.mark.asyncio
async def test_unavailable_keeper_refuses_even_a_plain_insert(memory_insert_keeper: Any) -> None:
    from server.core.leases import LeaseUnavailable

    memory_insert_keeper.down = True
    table: list[dict[str, Any]] = []
    with pytest.raises(LeaseUnavailable):
        await insert_memory_versions(
            [memory_repo.build_memory_version_row(_row())], ch=Replica(table)
        )
    assert table == []


@pytest.mark.asyncio
async def test_batch_contention_releases_keys_it_already_took(memory_insert_keeper: Any) -> None:
    from server.memory.store import write_fence

    batch = [memory_repo.build_memory_version_row(_row(), memory_key=key) for key in ("a", "b")]
    busy = write_fence.lease_name(rows.identity_key(batch[1]))
    memory_insert_keeper.seed(busy, "another-writer")
    table: list[dict[str, Any]] = []
    with pytest.raises(lease_writes.FenceUnavailableError):
        await insert_memory_versions(batch, ch=Replica(table))
    assert set(memory_insert_keeper.keys) == {busy}
    assert table == []


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
async def test_supported_batch_reaches_insert_with_live_holders(
    monkeypatch, no_blacklisted_values, restore
):
    """A bulk write or a large restore must reach each insert with live holders."""
    import asyncio
    from types import SimpleNamespace

    from server.core import lease_writes

    elapsed = 0.0
    held = []
    source = _row()
    count, latency = (10_000, 0.06) if restore else (1000, 0.03)
    batch = [memory_repo.build_memory_version_row(source, memory_key=f"k{i}") for i in range(count)]

    async def take(name, *, ttl_s, **kw):
        nonlocal elapsed
        started = elapsed
        await asyncio.sleep(0)
        elapsed = max(elapsed, started + latency)
        read = elapsed
        await asyncio.sleep(0)
        elapsed = max(elapsed, read + latency)
        lease = SimpleNamespace(
            name=name, acquired=started, ttl=ttl_s, released=False, let_expire=lambda: None
        )
        held.append(lease)
        return lease

    async def release(lease):
        lease.released = True

    def work_left(lease, ttl):
        return lease.acquired + ttl * 0.4 - elapsed

    class TimedInsert(Replica):
        async def insert(self, *args, **kw):
            assert all(work_left(lease, lease.ttl) > 0 for lease in held if not lease.released), (
                "batch admission consumed an earlier holder before the insert"
            )
            await super().insert(*args, **kw)

    monkeypatch.setattr(lease_writes.leases, "take", take)
    monkeypatch.setattr(lease_writes.leases, "release_shielded", release)
    monkeypatch.setattr(lease_writes.leases, "work_left", work_left)
    table = []
    if restore:
        from server.memory.app import snapshot_rows

        assert not await snapshot_rows.insert_version_rows(batch, ch=TimedInsert(table))
    else:
        await insert_memory_versions(batch, ch=TimedInsert(table))
    assert table == batch
    assert all(lease.released for lease in held)
