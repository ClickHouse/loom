"""Placement capacity is an opt-in, verified platform-admin health observation."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import health
from server.auth import revocation
from server.auth.jwt import mint_token, verify_token
from server.core import cell, pg, readiness
from server.core.schema import status as schema_status
from server.settings import settings

_CLIENT = httpx.AsyncClient
_TOKEN = "lcr_" + "0" * 128
_DEPLOYMENT = "deployment-eu-1"
_OBSERVED = "2026-01-01T00:00:00+00:00"


class _Router:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.answer: Any = {
            "cell": "eu-1",
            "deployment_id": _DEPLOYMENT,
            "placed_orgs": 7,
            "placements_max": 10,
            "closed": False,
            "observed_at": _OBSERVED,
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(self.status, json=self.answer)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> _Router:
    router = _Router()
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "cell_router_url", "https://router.test/")
    monkeypatch.setattr(settings, "cell_router_token", _TOKEN)
    monkeypatch.setattr(cell, "_deployment_id", _DEPLOYMENT)
    monkeypatch.setattr(
        cell,
        "_view",
        cell._View(
            read_at=cell.time.monotonic(),
            first=(False, "eu-1"),
            mode="enforced",
            current=cell.as_value(cell.mine()),
            has_tenancy=True,
        ),
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _CLIENT(transport=httpx.MockTransport(router.handle), **kwargs),
    )
    return router


@pytest.fixture
def app(router: _Router, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(health, "_ping_clickhouse", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(health, "_otel_unindexed_for_health", AsyncMock(return_value=None))
    monkeypatch.setattr(
        health,
        "_health_provider_fields",
        lambda *_args, **_kwargs: {"status": "ok", "fake_embedding": True},
    )
    monkeypatch.setattr(health, "_calibration_block", lambda *_args: {})
    monkeypatch.setattr(health, "_ratelimit_block", lambda: {})
    monkeypatch.setattr(health, "_deployment_config_block", lambda: {})
    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(schema_status, "schema_status", AsyncMock(return_value={}))
    monkeypatch.setattr(
        readiness, "evaluate", AsyncMock(return_value=readiness.Readiness({}, frozenset()))
    )
    app = FastAPI()
    health.attach(app)
    return app


def _headers(roles: list[str] | None = None, *, scoped: bool = False) -> dict[str, str]:
    token = mint_token(
        subject="capacity-reader",
        org="org",
        namespaces=["*"],
        roles=roles if roles is not None else ["platform_admin"],
        ns_roles={"private": "admin"} if scoped else None,
    )
    return {"Authorization": f"Bearer {token}"}


def _placement(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    answer = response.json()
    assert "capacity" in answer
    assert set(answer["capacity"]) == {"placement"}
    return dict(answer["capacity"]["placement"])


@pytest.mark.parametrize("maximum,closed", [(10, False), (None, True)])
def test_verified_admin_reads_only_current_own_cell_placement_evidence(
    app: FastAPI, router: _Router, maximum: int | None, closed: bool
) -> None:
    router.answer.update(placements_max=maximum, closed=closed)
    response = TestClient(app).get("/v1/health?include=capacity", headers=_headers())
    assert _placement(response) == {"status": "known", "source": "router", **router.answer}
    assert len(router.calls) == 1
    request = router.calls[0]
    assert str(request.url) == "https://router.test/cells/v1/cells/eu-1/capacity"
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert all(
        value == settings.cell_router_timeout_s for value in request.extensions["timeout"].values()
    )


@pytest.mark.parametrize(
    "caller", ["anonymous", "invalid", "writer", "org_admin", "scoped", "revoked", "unloaded"]
)
def test_unverified_or_scoped_callers_trigger_no_capacity_observation(
    app: FastAPI, router: _Router, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    headers = _headers()
    if caller == "anonymous":
        headers = {}
    elif caller == "invalid":
        headers = {"Authorization": "Bearer not-a-token"}
    elif caller in {"writer", "org_admin"}:
        headers = _headers([caller])
    elif caller == "scoped":
        headers = _headers(scoped=True)
    elif caller == "revoked":
        token_id = verify_token(headers["Authorization"].removeprefix("Bearer ")).token_id
        monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({token_id}))
    else:
        monkeypatch.setattr(revocation, "loaded", lambda: False)
    response = TestClient(app).get("/v1/health?include=capacity", headers=headers)
    assert response.status_code == 200
    assert "capacity" not in response.json()
    assert router.calls == []


def test_default_health_and_probes_do_not_read_capacity(app: FastAPI, router: _Router) -> None:
    client = TestClient(app)
    for path in ("/v1/health", "/v1/health/live", "/v1/health/ready"):
        response = client.get(path, headers=_headers())
        assert response.status_code == 200
        assert "capacity" not in response.json()
    assert router.calls == []


@pytest.mark.parametrize(
    "failure",
    [
        "old-router",
        "outage",
        "wrong-cell",
        "wrong-deployment",
        "bad-quota",
        "bad-time",
        "oversized",
        "timeout",
    ],
)
def test_unavailable_or_untrusted_router_evidence_never_becomes_a_zero_count(
    app: FastAPI, router: _Router, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "old-router":
        router.status = 404
    elif failure == "outage":
        router.status = 503
    elif failure == "wrong-cell":
        router.answer["cell"] = "other"
    elif failure == "wrong-deployment":
        router.answer["deployment_id"] = "other"
    elif failure == "bad-quota":
        router.answer["placements_max"] = False
    elif failure == "bad-time":
        router.answer["observed_at"] = "not-an-observation"
    elif failure == "timeout":

        async def unanswered(_request: httpx.Request) -> httpx.Response:
            router.calls.append(_request)
            await asyncio.sleep(0.1)
            return httpx.Response(200, json=router.answer)

        monkeypatch.setattr(router, "handle", unanswered)
        monkeypatch.setattr(settings, "cell_router_timeout_s", 0.01)
    else:

        class Oversized(httpx.AsyncByteStream):
            async def __aiter__(self) -> Any:
                yield b" " * 9000
                raise AssertionError("an oversized reply must close without reading the rest")

        async def oversized(request: httpx.Request) -> httpx.Response:
            router.calls.append(request)
            return httpx.Response(200, stream=Oversized())

        monkeypatch.setattr(router, "handle", oversized)
    placement = _placement(TestClient(app).get("/v1/health?include=capacity", headers=_headers()))
    assert placement["status"] == "unknown"
    assert placement["source"] == "router"
    assert set(placement) == {"status", "source", "reason"}
    assert len(router.calls) == 1


@pytest.mark.parametrize("missing", ["identity", "credential"])
def test_missing_own_identity_or_credential_reports_unknown_without_http(
    app: FastAPI, router: _Router, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    headers = _headers()
    if missing == "identity":
        monkeypatch.setattr(cell, "_deployment_id", "")
        monkeypatch.setattr(
            cell,
            "_view",
            cell._View(
                read_at=cell.time.monotonic(),
                first=(False, "eu-1"),
                mode="enforced",
                current=cell.as_value(cell.mine()),
                has_tenancy=True,
            ),
        )
    else:
        monkeypatch.setattr(settings, "cell_router_token", "")
    placement = _placement(TestClient(app).get("/v1/health?include=capacity", headers=headers))
    assert placement["status"] == "unknown"
    assert "placed_orgs" not in placement and "placements_max" not in placement
    assert router.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_capacity_observations_refuse_concurrency_and_reset_after_cancel(
    app: FastAPI, router: _Router, monkeypatch: pytest.MonkeyPatch, cancel_first: bool
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    handle = router.handle

    async def held(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return await handle(request)

    monkeypatch.setattr(router, "handle", held)
    transport = httpx.ASGITransport(app=app)
    async with _CLIENT(transport=transport, base_url="http://cell.test") as client:
        headers = _headers()
        first = asyncio.create_task(client.get("/v1/health?include=capacity", headers=headers))
        waiting = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait(
                {first, waiting}, timeout=2, return_when=asyncio.FIRST_COMPLETED
            )
            if first in done:
                assert "capacity" in first.result().json()
            assert waiting in done
            second = await asyncio.wait_for(
                client.get("/v1/health?include=capacity", headers=headers), timeout=2
            )
            assert _placement(second)["status"] == "unknown"
            assert router.calls == []
            if cancel_first:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                release.set()
                assert _placement(await first)["status"] == "known"
            release.set()
            assert (
                _placement(await client.get("/v1/health?include=capacity", headers=headers))[
                    "status"
                ]
                == "known"
            )
        finally:
            release.set()
            for task in (first, waiting):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, waiting, return_exceptions=True)
