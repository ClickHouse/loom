"""Own-cell capacity reads expose fresh placement limits and no directory secrets."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from starlette.testclient import TestClient

from server.router import app as router_app
from server.router import directory
from server.settings import settings
from tests.unit.clock_fake import ManualClock

TOKEN = "own-cell-credential-0123456789abcdef"
NEXT = "next-cell-credential-0123456789abcdef"
OBSERVED = datetime(2026, 1, 1, tzinfo=UTC)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _Directory:
    def __init__(self) -> None:
        self.row: directory.Cell | None = directory.Cell(
            cell="eu-1",
            is_first=False,
            host="eu-1.example.test",
            state="serving",
            mode="enforced",
            closed=True,
            placements_max=7,
            placed=7,
            credential_sha256=_sha(TOKEN),
            credential_next_sha256=_sha(NEXT),
            deployment_id="deployment-eu-1",
            current_fp={"private": "fingerprint-secret"},
            next_fp=None,
            mutation_generation=UUID(int=1),
        )
        self.reads: list[tuple[str, str]] = []
        self.credentials_down = False
        self.row_down = False

    async def credentials(self) -> dict[str, tuple[str, ...]]:
        if self.credentials_down:
            raise directory.DirectoryUnavailable("directory unavailable")
        return {"eu-1": (_sha(TOKEN), _sha(NEXT)), "us-1": (_sha("other-cell"),)}

    async def fetchrow(self, sql: str, name: str) -> dict[str, Any] | None:
        self.reads.append((sql, name))
        if self.row_down:
            raise OSError("private directory address")
        return {**dataclasses.asdict(self.row), "observed_at": OBSERVED} if self.row else None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Directory:
    fake = _Directory()

    @asynccontextmanager
    async def connection() -> Any:
        yield fake

    monkeypatch.setattr(directory, "credentials", fake.credentials)
    monkeypatch.setattr(directory, "_connection", connection)
    return fake


def _client() -> TestClient:
    return TestClient(router_app.build_app(router_app.Router(clock=ManualClock())))


def _read(client: TestClient, token: str = TOKEN) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/cells/v1/cells/eu-1/capacity", headers=headers)


@pytest.mark.parametrize("token", [TOKEN, NEXT])
def test_own_current_or_next_credential_reads_only_public_capacity(
    fake: _Directory, token: str
) -> None:
    response = _read(_client(), token)
    assert response.status_code == 200
    assert response.json() == {
        "cell": "eu-1",
        "deployment_id": "deployment-eu-1",
        "placed_orgs": 7,
        "placements_max": 7,
        "closed": True,
        "observed_at": OBSERVED.isoformat(),
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert len(fake.reads) == 1
    statement, name = fake.reads[0]
    assert name == "eu-1" and "WHERE cell = $1" in statement
    assert "loom_router.placements" not in statement
    assert "UPDATE " not in statement and "FOR UPDATE" not in statement


@pytest.mark.parametrize("token", ["", "other-cell", "operator-secret"])
def test_missing_other_cell_or_operator_credential_cannot_read_capacity(
    fake: _Directory, token: str
) -> None:
    response = _read(_client(), token)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert fake.reads == []


@pytest.mark.parametrize("change", ["revoked", "missing", "wrong-cell"])
def test_fresh_row_recheck_refuses_after_cached_authentication(
    fake: _Directory, change: str
) -> None:
    client = _client()
    assert _read(client).status_code == 200
    assert fake.row is not None
    if change == "missing":
        fake.row = None
    elif change == "wrong-cell":
        fake.row = dataclasses.replace(fake.row, cell="us-1")
    else:
        fake.row = dataclasses.replace(
            fake.row, credential_sha256=_sha("replacement"), credential_next_sha256=None
        )
    response = _read(client)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {"error"}
    assert len(fake.reads) == 2


@pytest.mark.parametrize("stage", ["credentials", "row"])
def test_directory_failure_never_returns_capacity_or_driver_secrets(
    fake: _Directory, stage: str
) -> None:
    fake.credentials_down = stage == "credentials"
    fake.row_down = stage == "row"
    client = _client()
    response = _read(client)
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {"error"}
    assert "private directory address" not in response.text
    if stage == "row":
        fake.row_down = False
        assert _read(client).status_code == 200


def test_capacity_reads_fresh_limits_without_reusing_the_state_cache(fake: _Directory) -> None:
    client = _client()
    assert _read(client).status_code == 200
    assert fake.row is not None
    fake.row = dataclasses.replace(fake.row, placed=8, placements_max=None, closed=False)
    response = _read(client)
    assert response.status_code == 200
    assert response.json()["placed_orgs"] == 8
    assert response.json()["placements_max"] is None
    assert response.json()["closed"] is False
    assert len(fake.reads) == 2


def test_capacity_reads_use_the_existing_fresh_read_budget(
    fake: _Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "router_lookups_per_minute", 2)
    client = _client()
    assert _read(client).status_code == 200
    assert _read(client).status_code == 200
    response = _read(client)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers["Cache-Control"] == "no-store"
    assert len(fake.reads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_only_one_capacity_observation_runs_without_a_waiter_queue(
    fake: _Directory, monkeypatch: pytest.MonkeyPatch, cancel_first: bool
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    fetchrow = fake.fetchrow

    async def held(sql: str, name: str) -> dict[str, Any] | None:
        entered.set()
        await release.wait()
        return await fetchrow(sql, name)

    monkeypatch.setattr(fake, "fetchrow", held)
    transport = httpx.ASGITransport(
        app=router_app.build_app(router_app.Router(clock=ManualClock()))
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://router.test") as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        first = asyncio.create_task(client.get("/cells/v1/cells/eu-1/capacity", headers=headers))
        waiting = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait(
                {first, waiting}, timeout=2, return_when=asyncio.FIRST_COMPLETED
            )
            if first in done:
                assert first.result().status_code == 200
            assert waiting in done
            second = await asyncio.wait_for(
                client.get("/cells/v1/cells/eu-1/capacity", headers=headers), timeout=2
            )
            assert second.status_code == 429
            assert second.headers["Cache-Control"] == "no-store"
            assert len(fake.reads) == 0
            if cancel_first:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                release.set()
            else:
                release.set()
                assert (await first).status_code == 200
            assert (
                await client.get("/cells/v1/cells/eu-1/capacity", headers=headers)
            ).status_code == 200
            if cancel_first:
                assert len(fake.reads) == 1
            else:
                assert len(fake.reads) == 2
        finally:
            release.set()
            for task in (first, waiting):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, waiting, return_exceptions=True)
