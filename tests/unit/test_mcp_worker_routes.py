"""Operator provisioning cannot be substituted by a database approval."""

import ssl
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.knowledge.app import mcp_worker_pg_directory
from server.knowledge.app.mcp_worker_routes import WorkerProfile, WorkerRoute, open_worker_routes

_PROFILE = "3" * 64


def _route() -> WorkerRoute:
    return WorkerRoute(
        worker_id=uuid4(),
        tls_origin="https://worker.example",
        tls=ssl.create_default_context(),
        issuer="loom",
        grant_key=Ed25519PrivateKey.generate(),
        enrollment_verifier=Ed25519PrivateKey.generate().public_key(),
        profiles=(WorkerProfile("org", _PROFILE, frozenset({"live"})),),
        max_live_requests=2,
    )


@pytest.mark.asyncio
async def test_database_approval_requires_exact_provisioned_route(monkeypatch) -> None:
    route = _route()
    current = AsyncMock(return_value=17)
    monkeypatch.setattr(mcp_worker_pg_directory.approvals, "current_generation", current)
    async with open_worker_routes((route,)) as routes:
        directory = mcp_worker_pg_directory.live_directory(routes)
        assert directory.worker(route.worker_id) is not None
        assert directory.worker(uuid4()) is None
        assert not await directory.worker_approval(
            "other", "docs", route.worker_id, _PROFILE, "live", False
        )
        assert not await directory.worker_approval(
            "org", "docs", route.worker_id, "f" * 64, "live", False
        )
        assert not await directory.worker_approval(
            "org", "docs", route.worker_id, _PROFILE, "live", True
        )
        assert not await directory.worker_approval(
            "org", "docs", route.worker_id, _PROFILE, "snapshot", False
        )
        current.assert_not_awaited()
        assert (
            await directory.worker_approval("org", "docs", route.worker_id, _PROFILE, "live", False)
            == 17
        )
        current.assert_awaited_once_with("org", "docs", route.worker_id, _PROFILE, "live", False)


@pytest.mark.asyncio
async def test_routes_reject_duplicate_workers_and_insecure_credentials() -> None:
    route = _route()
    insecure = ssl._create_unverified_context()
    for entries in (
        (route, route),
        (replace(route, tls=insecure),),
        (replace(route, enrollment_verifier=route.grant_key.public_key()),),
        (replace(route, profiles=(WorkerProfile("*", _PROFILE, frozenset({"live"})),)),),
    ):
        with pytest.raises(ValueError, match="invalid source worker (route|profile)"):
            async with open_worker_routes(entries):
                pytest.fail("invalid route opened")
