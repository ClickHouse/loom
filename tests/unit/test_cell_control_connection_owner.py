"""Control owns each raw session before deployment and codec I/O can wait."""

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

from server.cli import cell_control, cell_place
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
        return {"deployment_id": "control-deployment", "epoch": 7}

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
    monkeypatch.setattr(pg, "_expected", ("control-deployment", 7))
    monkeypatch.setattr(provider_process, "_identity", (os.getpid(), UUID(int=776)))
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://unused/control")
    monkeypatch.setattr(settings, "pg_statement_timeout_ms", 5000)
    monkeypatch.setattr(settings, "cell", "restore")

    def make():
        conn = Connection()
        owner = cell_control.Owner()
        conn.owner = owner

        async def connect(*args, **kwargs):
            return conn

        monkeypatch.setattr(asyncpg, "connect", connect)
        return owner, conn

    yield make
    assert gate.active == 0


@pytest.mark.parametrize("error_stage", [None, "deployment", "json"])
async def test_control_owner_binds_before_io_and_closes_initialization_failure(
    opening, monkeypatch, error_stage
):
    _, conn = opening()
    owner = cell_control.Owner()
    conn.owner = owner
    conn.error_stage = error_stage
    for name in ("direct_refusals", "encoding_refusals"):
        monkeypatch.setattr(cell_control.apg, name, AsyncMock(return_value=[]))
    monkeypatch.setattr(cell_control.apg, "take_lock", AsyncMock())
    try:
        if error_stage is None:
            await owner.call(lambda: owner._open("update"), total_s=2)
            assert not owner.closed and conn.closes == 0
        else:
            with pytest.raises(ValueError) as error:
                await owner.call(lambda: owner._open("update"), total_s=2)
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


async def control_outcome(owner):
    try:
        await owner.call(lambda: owner._open("update"), total_s=0.8)
    except asyncio.CancelledError as error:
        return error
    return None


@pytest.mark.parametrize("stage", ["deployment", "json"])
@pytest.mark.parametrize("ending", ["deadline", "cancel"])
async def test_control_public_close_precedes_initialization_cancellation(opening, stage, ending):
    owner, conn = opening()
    conn.block_stage = stage
    conn.close_allowed.clear()
    task = asyncio.create_task(control_outcome(owner))
    closing = asyncio.create_task(conn.close_started.wait())
    try:
        await asyncio.wait_for(conn.started.wait(), 1)
        if ending == "cancel":
            task.cancel("first cancellation")
        await asyncio.wait({task, closing}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
        assert conn.close_started.is_set(), "initialization must remain owned by public close"
        assert "initialization cancelled" not in conn.events
        conn.close_allowed.set()
        if ending == "deadline":
            with pytest.raises(TimeoutError, match="phase deadline expired"):
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
        conn.close_allowed.set()
        conn.release.set()
        for pending in (task, closing):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, closing, return_exceptions=True)


async def test_throwing_bind_keeps_raw_cleanup_with_connector(opening):
    owner, conn = opening()
    failure = ValueError("binding refused")

    def bind(raw):
        assert raw is conn
        raise failure

    with pytest.raises(ValueError) as error:
        await pg._connect_dedicated(bind=bind)
    assert error.value is failure
    assert owner.conn is None
    assert conn.io == [] and conn.closes == 0 and conn.terminations == 1


async def test_placement_uses_owned_connector_and_initializes_once(opening, monkeypatch):
    owner, conn = opening()
    stop = ValueError("stop before placement work")
    monkeypatch.setattr(cell_place, "Owner", lambda **kwargs: owner)
    monkeypatch.setattr(cell_place.apg, "direct_refusals", AsyncMock(side_effect=stop))
    with pytest.raises(ValueError) as error:
        await cell_place.place(
            argparse.Namespace(cell="control", org="acme"), httpx.URL("https://router.test")
        )
    assert error.value is stop
    assert conn.io == [("deployment", True, True), ("json", True, True), ("jsonb", True, True)]
    assert owner.conn is conn and owner.closed
    assert conn.closed and conn.closes == 1 and conn.terminations == 0
