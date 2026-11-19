"""Browser consent admits only the token or verified pending record's held org."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from server.auth import tenancy
from server.auth.oauth_store import token_id
from server.core import cell, pg
from server.core.errors import ValidationError
from server.knowledge.app import connection_routes as routes
from server.settings import settings
from server.transport.tenancy_gate import TenancyMiddleware
from tests.unit.test_connected_account_callbacks import (
    _primary_token,
    _request,
    _World,
)
from tests.unit.test_connected_account_callbacks import (
    world as world,
)


class _CellRows:
    def __init__(self) -> None:
        self.held = {"team"}
        self.down = False
        self.reads: list[str] = []
        self.placements: list[str] = []

    async def fetchval(self, query: str, org: str) -> Any:
        assert query == tenancy._HOLDS
        self.reads.append(org)
        if self.down:
            raise ConnectionError("cell registry unread")
        return 1 if org in self.held else None

    async def place(self, org: str) -> tuple[str, str]:
        self.placements.append(org)
        return "other-cell", "other-deployment"


@pytest.fixture
def cell_rows(world: _World, monkeypatch: pytest.MonkeyPatch) -> _CellRows:
    rows = _CellRows()
    monkeypatch.setattr(settings, "cell", "consent-cell")
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})
    monkeypatch.setattr(tenancy, "_not_held", {})
    monkeypatch.setattr(tenancy, "_placing", {})
    monkeypatch.setattr(pg, "fetchval", rows.fetchval)
    monkeypatch.setattr(tenancy, "_place", rows.place)
    monkeypatch.setattr(
        cell,
        "_view",
        cell._View(
            read_at=time.monotonic(),
            first=(False, settings.cell),
            mode="enforced",
            current={
                "v": cell.FINGERPRINT_VERSION,
                "fields": cell.fingerprint(cell.deployment_id()),
            },
            has_tenancy=True,
            marked=True,
        ),
    )
    monkeypatch.setattr(cell, "_memo", None)
    return rows


async def _start() -> httpx.Response:
    app = FastAPI()
    routes.attach(app)
    app.add_middleware(TenancyMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://app.example"
    ) as client:
        return await client.post(
            "/v1/connect/{kind}/start".format(kind="gdrive"),
            headers={"Authorization": "Bearer " + _primary_token()},
        )


@pytest.mark.parametrize("state", ["held", "unheld", "unread"])
async def test_consent_start_admits_token_org_before_pending_or_account_writes(
    world: _World, cell_rows: _CellRows, state: str
) -> None:
    if state == "unheld":
        cell_rows.held.clear()
    cell_rows.down = state == "unread"
    response = await _start()
    assert response.status_code == {"held": 200, "unheld": 409, "unread": 503}[state]
    assert cell_rows.reads == ["team"]
    assert not world.requests
    assert cell_rows.placements == (["team"] if state == "unheld" else [])
    if state != "held":
        assert world.rows == world.keeper == {}
        assert world.directory_reads == 0 and not world.audits


@pytest.mark.parametrize("mode", ["recording", "enforced"])
@pytest.mark.parametrize("state", ["held", "unheld", "unread"])
async def test_consent_callback_requires_verified_pending_org_held_without_placement(
    world: _World,
    cell_rows: _CellRows,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    state: str,
) -> None:
    response = await _start()
    assert response.status_code == 200
    query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    pending_state = query["state"][0]
    binding = response.cookies[routes._cookie(pending_state)]
    key = "connect:" + token_id(pending_state)
    pending = await routes.accounts.pending_store.pending(key)
    rows_before, keeper_before = dict(world.rows), dict(world.keeper)
    # Model delivery of the already-bound callback to another cell/process, which
    # has not observed the starting cell's immutable held-org row.
    tenancy._held.clear()
    cell_rows.reads.clear()
    if state == "unheld":
        monkeypatch.setattr(settings, "cell", "receiving-cell")
        cell_rows.held.clear()
    cell_rows.down = state == "unread"
    monkeypatch.setattr(cell, "mode", lambda: mode)
    if state == "held":
        done = await routes.callback("gdrive", _request("gdrive", pending_state, binding))
        assert done.status_code == 200 and world.account().active
        assert len(world.requests) == 2
        assert await routes.accounts.pending_store.pending(key) is None
    else:
        with pytest.raises(ValidationError):
            await routes.callback("gdrive", _request("gdrive", pending_state, binding))
        assert world.rows == rows_before and world.keeper == keeper_before
        assert not world.requests and not world.audits
        assert await routes.accounts.pending_store.pending(key) == pending
    assert cell_rows.reads == ["team"]
    assert not cell_rows.placements


async def test_consent_callback_checks_browser_binding_before_pending_org_tenancy(
    world: _World, cell_rows: _CellRows
) -> None:
    response = await _start()
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    before = dict(world.keeper)
    tenancy._held.clear()
    cell_rows.reads.clear()
    cell_rows.down = True
    with pytest.raises(ValidationError):
        await routes.callback("gdrive", _request("gdrive", state, "different-browser"))
    assert not cell_rows.reads and not cell_rows.placements
    assert world.keeper == before and not world.requests
