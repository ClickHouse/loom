"""Provider ingress bounds its bytes and acknowledges with the required status."""

from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.knowledge.app import source_push as push
from server.knowledge.app import source_push_routes as routes
from server.knowledge.domain.source_push import MAX_BODY_BYTES, DeliveryRefused, PushRegistration

URL = "/hooks/knowledge/docs/12345678-1234-1234-1234-123456789abc/22345678-1234-1234-1234-123456789abc?org=org"


def client() -> TestClient:
    app = FastAPI()
    routes.attach(app)
    return TestClient(app)


def test_verified_delivery_acknowledges_200_inside_provider_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receive = AsyncMock()
    monkeypatch.setattr(routes, "receive", receive)
    assert client().post(URL, content=b"signal").status_code == 200
    assert routes.CALLBACK_TIMEOUT_S < 5
    assert receive.call_args.args[:5] == (
        "org",
        "docs",
        "12345678-1234-1234-1234-123456789abc",
        "22345678-1234-1234-1234-123456789abc",
        b"signal",
    )


def test_oversized_delivery_never_reaches_store(monkeypatch: pytest.MonkeyPatch) -> None:
    receive = AsyncMock()
    monkeypatch.setattr(routes, "receive", receive)
    assert client().post(URL, content=b"x" * (MAX_BODY_BYTES + 1)).status_code == 413
    receive.assert_not_awaited()


def test_unverifiable_delivery_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        routes, "receive", AsyncMock(side_effect=DeliveryRefused("unverifiable delivery"))
    )
    assert client().post(URL, content=b"signal").status_code == 403


def test_callback_without_namespace_is_not_a_route(monkeypatch: pytest.MonkeyPatch) -> None:
    receive = AsyncMock()
    monkeypatch.setattr(routes, "receive", receive)
    assert client().post(URL.replace("/docs/", "/"), content=b"signal").status_code == 404
    receive.assert_not_awaited()


@pytest.fixture
def callback_env(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    held = AsyncMock(return_value=True)
    monkeypatch.setattr(routes.tenancy, "is_held", held)
    monkeypatch.setattr(push.settings, "public_base_url", "https://loom.example")
    return held


def registration(org: str) -> PushRegistration:
    return PushRegistration(
        org=org,
        namespace="docs",
        name="source",
        source_id="12345678-1234-1234-1234-123456789abc",
        generation="22345678-1234-1234-1234-123456789abc",
        provider="notion",
        fingerprint="fingerprint",
    )


@pytest.mark.parametrize(
    "org",
    ["org", "team/acme", "team?x=1&org=other#group", "team%2Facme+ ops", "研究/チーム &é"],
)
def test_generated_callback_round_trips_the_exact_organization_through_asgi(
    monkeypatch: pytest.MonkeyPatch, callback_env: AsyncMock, org: str
) -> None:
    receive = AsyncMock()
    monkeypatch.setattr(routes, "receive", receive)
    reg = registration(org)
    response = client().post(push.callback_url(reg), content=b"signal")
    assert response.status_code == 200
    callback_env.assert_awaited_once_with(org)
    assert receive.await_args.args[:5] == (
        org,
        reg.namespace,
        reg.source_id,
        reg.generation,
        b"signal",
    )


def test_callback_requires_organization_before_tenancy_or_delivery(
    monkeypatch: pytest.MonkeyPatch, callback_env: AsyncMock
) -> None:
    receive = AsyncMock()
    monkeypatch.setattr(routes, "receive", receive)
    address = urlsplit(push.callback_url(registration("org")))
    missing_org = urlunsplit(address._replace(query=""))
    assert client().post(missing_org, content=b"signal").status_code == 422
    callback_env.assert_not_awaited()
    receive.assert_not_awaited()
