"""Refusing an unsent checkout does not destroy another person's provider grant."""

from __future__ import annotations

import time

import httpx
import pytest

from server.core import request_work
from server.core.errors import ValidationError
from server.core.secrets_at_rest import unseal
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.domain.connected_accounts import CREDENTIAL_PURPOSE
from server.operations.base import Role
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_authority import _KEY, _REFRESH
from tests.unit.test_connected_source_operations import _operator, _World


async def test_presenter_lost_before_refresh_dispatch_releases_only_its_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider(request: httpx.Request) -> httpx.Response:
        return await state.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        state = _World(monkeypatch, http)
        original = state.rows[_KEY]
        put = state.put

        async def persisted(row, **kwargs):
            await put(row, **kwargs)
            if row.active and row.refresh_id:
                state.roles["operator"] = Role.VIEWER

        monkeypatch.setattr(accounts.store, "put", persisted)
        assert state.source is not None
        with (
            request_work.bind(request_work.RequestWork()),
            acting_as(_operator(Role.VIEWER)),
            pytest.raises(ValidationError, match="^the connected account is unavailable$"),
        ):
            await accounts.resolve_probe(state.source, deadline=time.monotonic() + 30)
        current = state.rows[_KEY]
        assert current.active and not current.refresh_id
        assert current.connection_id == original.connection_id
        assert unseal(CREDENTIAL_PURPOSE, current.credential_enc) == _REFRESH
        assert state.source is not None and state.source.active
        assert state.refresh_requests == state.native_requests == []
        assert state.source_writes == state.retirement_rows == []
