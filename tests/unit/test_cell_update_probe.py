"""A configuration or address proof cannot leave an unbounded HTTP waiter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.cli import cell_control, cell_update_probe
from tests.unit.test_cell_update_run import row


@pytest.mark.asyncio
async def test_update_proof_has_whole_reply_deadline_and_exact_nonce_cleanup(monkeypatch):
    class Direct:
        conn = object()

        async def call(self, work, *, total_s):
            return await work()

    nonce = {"postgres": "pg", "clickhouse": "ch", "bucket": None}
    monkeypatch.setattr(cell_control, "check_lock", AsyncMock())
    monkeypatch.setattr(cell_update_probe.cell_prepare, "_new_nonces", lambda: nonce)
    monkeypatch.setattr(cell_update_probe.cell_prepare, "_write_nonces", AsyncMock())
    cleanup = AsyncMock()
    monkeypatch.setattr(cell_update_probe.cell_prepare, "_delete_nonces", cleanup)
    monkeypatch.setattr(cell_update_probe, "ANSWER_S", 0.02, raising=False)
    cancelled = asyncio.Event()

    async def stalled(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(cell_update_probe.cell_prepare, "_register", stalled)
    run = SimpleNamespace(
        owner=Direct(),
        args=SimpleNamespace(wait=0),
        ch=object(),
        router=None,
        transport=None,
        row=row(),
    )
    task = asyncio.create_task(cell_update_probe.post(run, "/configuration", {}))
    done, _ = await asyncio.wait({task}, timeout=0.15)
    if not done:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pytest.fail("update proof exceeded its whole reply deadline")
    with pytest.raises(TimeoutError):
        await task
    assert cancelled.is_set()
    cleanup.assert_awaited_once_with(run.ch, nonce)


@pytest.mark.asyncio
async def test_router_without_update_route_names_the_required_upgrade():
    import httpx

    from server.cli import cell_prepare

    with pytest.raises(cell_prepare.Refused, match="not upgraded for cell update"):
        await cell_prepare._register(
            SimpleNamespace(cell="first", wait=0),
            httpx.URL("http://router"),
            {},
            httpx.MockTransport(lambda _: httpx.Response(404, text="Not Found")),
            path="/configuration",
            command="update",
        )
