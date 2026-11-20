"""A scan cannot write tenancy after its exact ATTACH backend has ended."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.cli import cell_attach, cell_control, cell_scan
from server.cli import cell_attach_pg as apg
from server.core import cell
from server.settings import settings
from tests.unit.cell_control_owner import owner
from tests.unit.test_cell_attach import CURRENT, NEXT, FakeConn, _args
from tests.unit.test_cell_control import snapshot


class Store:
    def __init__(self, phase: str, interruption: str):
        self.mode = "recording" if phase == "recording" else "enforced"
        self.first = {"is_first": True, "cell": "first"}
        self.own = {"current_fp": CURRENT, "next_fp": NEXT}
        self.tenancy = {"org-0000"}
        self.guard = True
        self.holder = "attach-1"
        self.row = {**snapshot(), **self.own, "state": "attaching", "placed_orgs": 0}
        self.phase, self.interruption = phase, interruption
        self.paused, self.resume = asyncio.Event(), asyncio.Event()
        self.batches: list[tuple[str, list[str]]] = []
        self.closed: list[str] = []
        self.claims = 0
        self.commits = 0

    async def call(self, path: str, body: Any = None):
        if not path:
            return dict(self.row)
        assert (
            body["expected"]
            == cell_control.preconditions(self.row, claim=path == "/claim")["expected"]
        )
        if path == "/claim":
            self.claims += 1
            self.row["mutation_generation"] = str(UUID(int=self.claims + 1))
        elif path == "/limit":
            self.row["closed"] = body["closed"]
        elif path == "/attach/place":
            self.row["placed_orgs"] += len(body["orgs"])
            return {**self.row, "placed": len(body["orgs"])}
        else:
            assert path == "/attach/enforced"
        return dict(self.row)

    async def pause_after_batch(self):
        if self.interruption == "between" and len(self.batches) == 1:
            self.paused.set()
            await self.resume.wait()


class Session(FakeConn):
    def __init__(self, store: Store, identity: str):
        super().__init__()
        self.store, self.identity = store, identity
        self.dead = False
        self.pending: set[str] | None = None
        self.has_insert = False

    def alive(self):
        if self.dead:
            raise ConnectionError("exact ATTACH backend ended")
        if self.identity != "pool" and self.store.holder != self.identity:
            raise ConnectionError("exact ATTACH backend no longer owns its session")

    def _answer(self, sql: str, args: tuple[Any, ...]):
        self.alive()
        store = self.store
        if sql == apg._HELD:
            return store.holder == self.identity
        if sql == cell._STATE:
            return {
                "is_first": store.first["is_first"] if store.first else None,
                "cell": store.first["cell"] if store.first else None,
                "mode": store.mode,
                "has_own": store.own is not None,
                "has_tenancy": bool(store.tenancy),
            }
        if sql == "SELECT deployment_id FROM loom.authority":
            return "dep"
        if sql == "SELECT mode FROM loom.tenancy_mode":
            return store.mode
        if sql == "SELECT is_first, cell FROM loom.cell_first":
            return store.first
        if sql == "SELECT current_fp, next_fp FROM loom.cell_self":
            return store.own
        if sql == "SELECT EXISTS(SELECT 1 FROM loom.tenancy)":
            return bool(store.tenancy)
        if "FROM pg_trigger" in sql:
            return [{"tgname": "tenancy_record", "relname": "memory_current"}]
        return super()._answer(sql, args)

    async def execute(self, sql: str, *args: Any):
        self.alive()
        store = self.store
        if sql == "BEGIN":
            self.pending = set(store.tenancy)
        elif sql.startswith("INSERT INTO loom.tenancy"):
            batch = list(args[0])
            target = store.tenancy if self.pending is None else self.pending
            added = len(set(batch) - target)
            target.update(batch)
            store.batches.append((self.identity, batch))
            self.has_insert = True
            if self.pending is None:
                await store.pause_after_batch()
            return f"INSERT 0 {added}"
        elif sql == "COMMIT":
            if self.pending is not None:
                store.tenancy = self.pending
                self.pending = None
            if self.has_insert:
                self.has_insert = False
                store.commits += 1
                if store.interruption == "unknown":
                    raise OSError("tenancy COMMIT reply lost")
                await store.pause_after_batch()
        elif sql == "ROLLBACK":
            self.pending = None
        elif sql.startswith("DELETE FROM loom."):
            table = sql.removeprefix("DELETE FROM loom.")
            if table == "tenancy":
                store.tenancy.clear()
                if self.pending is not None:
                    self.pending.clear()
            else:
                assert table in {"cell_first", "tenancy_mode", "cell_self"}
                field = {"cell_first": "first", "tenancy_mode": "mode", "cell_self": "own"}[table]
                setattr(store, field, None)
        elif sql.startswith("DROP TRIGGER"):
            store.guard = False
        return await super().execute(sql, *args)

    def lose(self):
        self.dead = True
        self.pending = None
        if self.store.holder == self.identity:
            self.store.holder = ""

    async def close(self, **kwargs: Any):
        self.store.closed.append(self.identity)
        self.lose()
        await super().close(**kwargs)

    def is_closed(self):
        return self.dead


def prepare(monkeypatch, phase: str, interruption: str, count: int):
    store = Store(phase, interruption)
    raw, pooled = Session(store, "attach-1"), Session(store, "pool")
    orgs = [f"org-{i:04}" for i in range(count)]
    found = cell_scan.ScanResult(
        orgs={org: {"postgres loom.memory_current"} for org in reversed(orgs)}
    )
    logs: list[str] = []
    run = cell_attach.Run(AsyncMock(), _args(), SimpleNamespace(call=store.call), logs.append)
    run.own_fp = (CURRENT, NEXT)
    run.read_own_row = AsyncMock()
    run.refusals = AsyncMock(return_value=(None, None, None))
    run.begin = AsyncMock()
    run.precommit = AsyncMock()
    run.durable = AsyncMock(return_value=100)
    run.check_beats = AsyncMock()
    run.cheap_checks = AsyncMock()
    run.nonheartbeat_checks = AsyncMock()
    run.final_checks = AsyncMock()
    run.serving = AsyncMock(return_value={"state": "serving", "placed_orgs": count})

    async def opened(owned, what):
        assert what == "attach"
        owned.conn = raw
        await cell_control.check_lock(raw)

    async def scan(*args):
        if interruption == "scan":
            store.paused.set()
            await store.resume.wait()
        return found

    async def enforced():
        store.mode, run.mode = "enforced", "enforced"

    async def triggers():
        await cell_control.check_lock(raw)

    async def pooled_execute(sql, *args):
        return await pooled.execute(sql, *args)

    @asynccontextmanager
    async def dedicated(**kwargs):
        yield raw

    async def watch(_self):
        await asyncio.Future()

    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    monkeypatch.setattr(cell_control.Owner, "_open", opened)
    monkeypatch.setattr(apg.pg, "execute", pooled_execute)
    monkeypatch.setattr(apg.pg, "dedicated", dedicated)
    monkeypatch.setattr(apg, "wait_transactions", AsyncMock())
    monkeypatch.setattr(cell_scan, "list_tables", AsyncMock(return_value=None))
    monkeypatch.setattr(cell_attach.beats.Watch, "run", watch)
    run.scan, run.triggers, run.commit_mode = scan, triggers, enforced
    if phase == "recording":
        run.step4 = AsyncMock()
    return store, raw, run, orgs, logs


async def lose_and_clean(store: Store, raw: Session):
    raw.lose()
    if store.phase == "recording":
        store.holder = "cleanup-2"
        cleanup = Session(store, "cleanup-2")
        cleanup_owner = owner(cleanup)
        try:
            await cell_control.backout(cleanup_owner, store.call, dict(store.row))
        finally:
            await cleanup_owner.close()
        assert store.tenancy == set()
        assert (store.mode, store.first, store.own, store.guard) == (None, None, None, False)
        assert store.row["closed"] is True and store.row["placed_orgs"] == 0
    # First-cell enforced backout is forbidden: step 4 tests only backend loss.
    return set(store.tenancy)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["recording", "step4"])
@pytest.mark.parametrize("interruption", ["scan", "between"])
async def test_lost_attach_cannot_write_tenancy_after_its_scan_or_first_batch(
    monkeypatch, phase, interruption
):
    store, raw, run, orgs, _ = prepare(monkeypatch, phase, interruption, 2001)
    task = asyncio.create_task(run.run())
    paused = asyncio.create_task(store.paused.wait())
    try:
        await asyncio.wait({task, paused}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            await task
        assert store.paused.is_set()
        preserved = await lose_and_clean(store, raw)
        batches_at_loss = len(store.batches)
        store.resume.set()
        with pytest.raises((cell_control.Refused, ConnectionError)):
            await task
        assert store.tenancy == preserved
        assert len(store.batches) == batches_at_loss
        assert (set(orgs[batches_at_loss * 1000 :]) - preserved).isdisjoint(store.tenancy)
        if phase == "recording":
            assert (store.mode, store.first, store.own) == (None, None, None)
            assert not store.tenancy and not store.guard
            assert await cell.read_state(Session(store, "pool")) == "open"
    finally:
        store.resume.set()
        if not task.done():
            task.cancel()
        if not paused.done():
            paused.cancel()
        await asyncio.gather(task, paused, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["recording", "step4"])
async def test_healthy_attach_keeps_sorted_thousand_org_batches_and_insert_count(
    monkeypatch, phase
):
    store, _, run, orgs, logs = prepare(monkeypatch, phase, "healthy", 2001)
    assert await run.run() == 0
    assert store.tenancy == set(orgs)
    assert [org for _, batch in store.batches for org in batch] == orgs
    assert [len(batch) for _, batch in store.batches] == [1000, 1000, 1]
    assert store.claims == 1 and store.closed.count("attach-1") == 1
    if phase == "recording":
        assert "step 2: 2001 orgs found, 2000 tenancy rows written" in logs
    else:
        assert run.report["tenancy_rows_written"] == 2000
        assert run.report["orgs_found"] == 2001
        assert run.report["placed_now"] == 2001


@pytest.mark.asyncio
async def test_unknown_tenancy_commit_stops_before_a_later_batch_without_replay(monkeypatch):
    store, _, run, orgs, _ = prepare(monkeypatch, "recording", "unknown", 2001)
    error = None
    try:
        await run.run()
    except OSError as caught:
        error = caught
    assert store.tenancy == set(orgs[:1000])
    assert len(store.batches) == 1
    assert str(error) == "tenancy COMMIT reply lost"
    assert store.closed.count("attach-1") == 1
    assert store.claims == 1
