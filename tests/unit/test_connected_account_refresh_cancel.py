"""Cancellation of an unsent reserved checkout leaves the owner's grant usable."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from server.core import request_work
from server.core.secrets_at_rest import unseal
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.domain.connected_accounts import CREDENTIAL_PURPOSE
from server.operations.base import Role
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_authority import _KEY, _REFRESH
from tests.unit.test_connected_source_operations import _operator, _World


@pytest.mark.parametrize("stage", ("store-write", "lease-exit"))
async def test_cancellation_after_reservation_write_releases_only_the_unsent_checkout(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    async def provider(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the cancelled reservation must never reach the provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        state = _World(monkeypatch, http)
        original = state.rows[_KEY]
        persisted = asyncio.Event()
        release = asyncio.Event()
        put = state.put

        async def paused_put(row, **kwargs):
            await put(row, **kwargs)
            if stage == "store-write" and row.active and row.refresh_id:
                persisted.set()
                await release.wait()

        paused = False

        @asynccontextmanager
        async def paused_lease(key, **kwargs):
            nonlocal paused
            async with state.lease(key, **kwargs):
                yield
                if stage == "lease-exit" and not paused and state.rows[_KEY].refresh_id:
                    paused = True
                    persisted.set()
                    await release.wait()

        monkeypatch.setattr(accounts.store, "put", paused_put)
        monkeypatch.setattr(accounts.leases, "cluster_lease", paused_lease)
        assert state.source is not None
        with request_work.bind(request_work.RequestWork()), acting_as(_operator(Role.VIEWER)):
            work = asyncio.create_task(
                accounts.resolve_probe(state.source, deadline=time.monotonic() + 30)
            )
            try:
                await state.await_stage(persisted, work)
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await work
            finally:
                release.set()
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
        current = state.rows[_KEY]
        assert current.active and not current.refresh_id
        assert current.connection_id == original.connection_id
        assert unseal(CREDENTIAL_PURPOSE, current.credential_enc) == _REFRESH
        assert state.source is not None and state.source.active
        assert state.refresh_requests == state.native_requests == []
        assert state.source_writes == state.retirement_rows == []
