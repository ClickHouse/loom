"""Restore owns each raw session before deployment and codec I/O can wait."""

from __future__ import annotations

import argparse
import asyncio
import os
import weakref
from unittest.mock import AsyncMock
from uuid import UUID

import asyncpg
import httpx
import pytest

from server.cli import cell_control, cell_restore
from server.core import pg, provider_process, retirement
from server.settings import settings


class Connection:
    def __init__(self):
        self.owner = None
        self.io = []
        self.events = []
        self.block_stage = None
        self.error_stage = None
        self.failure = ValueError("initialization refused")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_allowed = asyncio.Event()
        self.close_allowed.set()
        self.closes = 0
        self.terminations = 0
        self.closed = False

    async def step(self, stage):
        cleanup_owner = getattr(self.owner, "budget", self.owner)
        self.io.append((stage, self.owner.conn is self, cleanup_owner.conn is self))
        if stage == self.error_stage:
            raise self.failure
        if stage == self.block_stage:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.events.append("initialization cancelled")
                raise

    async def fetchrow(self, sql):
        assert sql == "SELECT epoch, deployment_id FROM loom.authority"
        await self.step("deployment")
        return {"deployment_id": "restore-deployment", "epoch": 7}

    async def set_type_codec(self, typ, **kwargs):
        assert kwargs["schema"] == "pg_catalog"
        await self.step(typ)

    async def close(self, *, timeout):  # noqa: ASYNC109 — matches the driver close API.
        assert timeout > 0
        self.closes += 1
        self.events.append("public close started")
        self.close_started.set()
        await self.close_allowed.wait()
        self.closed = True
        self.events.append("public close finished")

    def terminate(self):
        self.terminations += 1

    def is_closed(self):
        return self.closed or bool(self.terminations)


@pytest.fixture
def opening(monkeypatch):
    gate = retirement.RetirementGate()
    monkeypatch.setattr(retirement, "_gate", gate)
    monkeypatch.setattr(retirement, "_pid", os.getpid())
    monkeypatch.setattr(pg, "_state_pid", os.getpid())
    for name in ("_pools", "_opening", "_closing"):
        monkeypatch.setattr(pg, name, weakref.WeakKeyDictionary())
    monkeypatch.setattr(pg, "_expected", ("restore-deployment", 7))
    monkeypatch.setattr(provider_process, "_identity", (os.getpid(), UUID(int=796)))
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://unused/restore")
    monkeypatch.setattr(settings, "pg_statement_timeout_ms", 5000)
    monkeypatch.setattr(settings, "cell", "restore")

    def make(seconds=2):
        conn = Connection()
        owner = cell_restore.Restore(
            argparse.Namespace(), httpx.URL("https://router.test"), cell_restore.Budget(seconds)
        )
        conn.owner = owner

        async def connect(*args, **kwargs):
            return conn

        monkeypatch.setattr(asyncpg, "connect", connect)
        return owner, conn

    yield make
    assert gate.active == 0


async def open_outcome(owner):
    try:
        await owner.budget.call(owner.open())
    except asyncio.CancelledError as error:
        return error
    return None


async def settle(conn, task, *others):
    conn.close_allowed.set()
    conn.release.set()
    for pending in (task, *others):
        if not pending.done():
            pending.cancel()
    await asyncio.gather(task, *others, return_exceptions=True)


async def test_restore_owns_raw_session_before_initialization_and_initializes_once(opening):
    owner, conn = opening()
    try:
        await owner.budget.call(owner.open())
        assert conn.io == [("deployment", True, True), ("json", True, True), ("jsonb", True, True)]
        assert owner.conn is conn and owner.budget.conn is conn
    finally:
        await owner.budget.close()
    assert conn.closed and conn.closes == 1 and conn.terminations == 0


@pytest.mark.parametrize("stage", ["deployment", "json"])
@pytest.mark.parametrize("ending", ["deadline", "cancel", "second_cancel"])
async def test_initialization_closes_publicly_before_cancelling_work(opening, stage, ending):
    owner, conn = opening(0.8 if ending == "deadline" else 2)
    conn.block_stage = stage
    conn.close_allowed.clear()
    task = asyncio.create_task(open_outcome(owner))
    closing = asyncio.create_task(conn.close_started.wait())
    try:
        await asyncio.wait_for(conn.started.wait(), 1)
        if ending != "deadline":
            task.cancel("first cancellation")
        await asyncio.wait({task, closing}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
        assert conn.close_started.is_set(), "initialization must remain owned by public close"
        assert "initialization cancelled" not in conn.events
        if ending == "second_cancel":
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not task.done()
        conn.close_allowed.set()
        if ending == "deadline":
            with pytest.raises(TimeoutError, match="cell restore reached --wait"):
                await asyncio.wait_for(asyncio.shield(task), 1)
        else:
            error = await asyncio.wait_for(asyncio.shield(task), 1)
            assert isinstance(error, asyncio.CancelledError)
            assert error.args == ("first cancellation",)
        assert conn.events == [
            "public close started",
            "public close finished",
            "initialization cancelled",
        ]
        assert conn.closed and conn.closes == 1 and conn.terminations == 0
    finally:
        await settle(conn, task, closing)


@pytest.mark.parametrize("stage", ["deployment", "json"])
async def test_initialization_error_keeps_original_error_after_public_close(opening, stage):
    owner, conn = opening()
    conn.error_stage = stage
    with pytest.raises(ValueError) as error:
        await owner.budget.call(owner.open())
    assert error.value is conn.failure
    assert conn.closed and conn.closes == 1 and conn.terminations == 0
    assert all(restore_owned and budget_owned for _, restore_owned, budget_owned in conn.io)


@pytest.mark.parametrize("ending", ["deadline", "cancel"])
async def test_late_raw_driver_connection_is_closed_without_initialization(
    opening, monkeypatch, ending
):
    owner, conn = opening(0.8 if ending == "deadline" else 2)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def connect(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        return conn

    monkeypatch.setattr(asyncpg, "connect", connect)
    task = asyncio.create_task(open_outcome(owner))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if ending == "cancel":
            task.cancel("opening cancelled")
            error = await asyncio.wait_for(asyncio.shield(task), 1)
            assert isinstance(error, asyncio.CancelledError)
            assert error.args == ("opening cancelled",)
        else:
            with pytest.raises(TimeoutError, match="cell restore reached --wait"):
                await asyncio.wait_for(asyncio.shield(task), 1)
        assert owner.budget.closed
        assert owner.conn is conn and owner.budget.conn is conn
        assert conn.io == []
        assert conn.closed and conn.closes == 1 and conn.terminations == 0
    finally:
        release.set()
        await settle(conn, task)


async def test_throwing_bind_leaves_raw_cleanup_with_connector(opening):
    owner, conn = opening()
    failure = ValueError("binding refused")

    def bind(raw):
        assert raw is conn
        raise failure

    with pytest.raises(ValueError) as error:
        await pg._connect_dedicated(bind=bind)
    assert error.value is failure
    assert owner.conn is None and owner.budget.conn is None
    assert conn.io == [] and conn.closes == 0 and conn.terminations == 1


@pytest.mark.parametrize("sealing", ["before_initialization", "after_codecs"])
async def test_bound_restore_refuses_retirement_seal_and_closes_publicly(
    opening, monkeypatch, sealing
):
    owner, conn = opening()
    gate = retirement.get_gate()

    async def connect(*args, **kwargs):
        if sealing == "before_initialization":
            gate.pg_sealed = True
        return conn

    async def codec(typ, **kwargs):
        assert kwargs["schema"] == "pg_catalog"
        await conn.step(typ)
        if typ == "jsonb":
            gate.pg_sealed = True

    monkeypatch.setattr(asyncpg, "connect", connect)
    if sealing == "after_codecs":
        monkeypatch.setattr(conn, "set_type_codec", codec)
    with pytest.raises(retirement.ProcessStopping):
        await owner.budget.call(owner.open())
    assert owner.conn is conn and owner.budget.conn is conn
    assert owner.budget.closed and conn.closed
    assert conn.closes == 1 and conn.terminations == 0
    assert conn.io == (
        []
        if sealing == "before_initialization"
        else [("deployment", True, True), ("json", True, True), ("jsonb", True, True)]
    )


@pytest.mark.parametrize("error_stage", [None, "deployment", "json"])
async def test_control_owner_binds_before_io_and_closes_initialization_failure(
    opening, error_stage
):
    _, conn = opening()
    owner = cell_control.Owner()
    conn.owner = owner
    conn.error_stage = error_stage
    try:
        if error_stage is None:
            await owner.connect(statement_timeout_ms=5000)
            assert not owner.closed and conn.closes == 0
        else:
            with pytest.raises(ValueError) as error:
                await owner.connect(statement_timeout_ms=5000)
            assert error.value is conn.failure
            assert owner.closed and conn.closed and conn.closes == 1
        stages = ["deployment", "json", "jsonb"]
        if error_stage is not None:
            stages = stages[: stages.index(error_stage) + 1]
        assert conn.io == [(stage, True, True) for stage in stages]
        assert owner.conn is conn and conn.terminations == 0
    finally:
        await owner.close()
    assert conn.closed and conn.closes == 1 and conn.terminations == 0


@pytest.mark.parametrize("state", ["closed", "bound"])
async def test_control_owner_refuses_reuse_before_driver_acquisition(opening, monkeypatch, state):
    _, conn = opening()
    owner = cell_control.Owner()
    owner.closed = state == "closed"
    owner.conn = conn if state == "bound" else None
    connect = AsyncMock()
    monkeypatch.setattr(asyncpg, "connect", connect)
    try:
        with pytest.raises(cell_control.Refused):
            await owner.connect(statement_timeout_ms=5000)
        connect.assert_not_awaited()
        assert owner.conn is (conn if state == "bound" else None)
    finally:
        await owner.close()
