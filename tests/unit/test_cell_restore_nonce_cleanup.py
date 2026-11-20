"""Restore probe failures remain first when nonce cleanup is interrupted."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.cli import cell_control
from server.cli import cell_restore_stores as stores
from server.core import leases, object_store
from server.settings import settings


@pytest.fixture
def nonce_probe(monkeypatch):
    state = SimpleNamespace(
        first=None, secondary=None, phase="clickhouse", cleanup=False, events=[]
    )
    nonce = "owned-restore-nonce"
    monkeypatch.setattr(stores, "secrets", SimpleNamespace(token_hex=lambda count: nonce))
    monkeypatch.setattr(settings, "document_s3_bucket", "restore-nonce-test")
    monkeypatch.setattr(object_store, "root_prefix", lambda: "")

    async def transaction(conn, work):
        return await work()

    async def execute(sql, *args):
        if sql == leases.LOCK_RELEASE:
            state.cleanup = True
            state.events.append("clickhouse cleanup")
            if state.phase == "clickhouse" and state.secondary is not None:
                raise state.secondary

    @asynccontextmanager
    async def transfer():
        state.events.append("bucket transfer enter")
        try:
            yield object()
        finally:
            state.events.append("bucket transfer exit")

    async def read(location):
        if not state.cleanup and state.first is not None:
            raise state.first
        return nonce.encode()

    async def delete(bucket, keys):
        state.events.append("bucket cleanup")
        if state.phase == "bucket" and state.secondary is not None:
            raise state.secondary

    monkeypatch.setattr(stores.local, "transaction", transaction)
    monkeypatch.setattr(object_store, "transfer", transfer)
    monkeypatch.setattr(object_store, "put", AsyncMock())
    monkeypatch.setattr(object_store, "read", read)
    monkeypatch.setattr(object_store, "delete", delete)
    state.ch = SimpleNamespace(execute=execute, query_one=AsyncMock(return_value={"holder": nonce}))
    state.conn = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(return_value=nonce))
    return state


async def outcome(state):
    try:
        result = await cell_control._captured(lambda: stores.nonces(state.ch, state.conn))
    except BaseException as error:  # noqa: BLE001 — compare every first outcome by identity.
        return error
    return result.error if isinstance(result, cell_control._Fatal) else result


@pytest.mark.parametrize(
    "first_type", [ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]
)
@pytest.mark.parametrize("secondary_type", [asyncio.CancelledError, SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("phase", ["clickhouse", "bucket"])
async def test_probe_failure_survives_fatal_nonce_cleanup(
    nonce_probe, first_type, secondary_type, phase
):
    state = nonce_probe
    state.first = first_type("original probe outcome")
    state.secondary = secondary_type("later cleanup outcome")
    state.phase = phase
    observed = await outcome(state)
    assert observed is state.first
    assert "clickhouse cleanup" in state.events
    assert "bucket transfer exit" in state.events
    if phase == "bucket":
        assert "bucket cleanup" in state.events


@pytest.mark.parametrize("secondary_type", [asyncio.CancelledError, SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("phase", ["clickhouse", "bucket"])
async def test_successful_probe_keeps_its_first_fatal_cleanup(nonce_probe, secondary_type, phase):
    state = nonce_probe
    state.secondary = secondary_type("cleanup is the first outcome")
    state.phase = phase
    assert await outcome(state) is state.secondary


@pytest.mark.parametrize("phase", ["clickhouse", "bucket"])
async def test_ordinary_nonce_cleanup_error_remains_best_effort(nonce_probe, phase):
    state = nonce_probe
    state.secondary = RuntimeError("ordinary cleanup failure")
    state.phase = phase
    assert await outcome(state) is None
    assert "clickhouse cleanup" in state.events
    assert "bucket cleanup" in state.events
