"""Both SCIM route families use the same local cell identity and token policy."""

from __future__ import annotations

import functools
import hmac
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import scim, scim_routes
from server.auth.jwt import AuthUnavailable, mint_token, verify_token
from server.core import cell
from server.identity.app import scim_groups
from server.settings import settings
from tests.unit.test_scim_groups import store as store

OWN = "lsc_first_" + "a" * 64
NEXT = "lsc_first_" + "b" * 64
FOREIGN = "lsc_another_" + "c" * 64
LEGACY = "legacy-token-" + "x" * 40
JWT_SECRET = "unit-cell-token-secret-" * 3


@pytest.fixture
def identity(monkeypatch):
    first = [True, "first"]
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "scim_token", OWN)
    monkeypatch.setattr(
        cell,
        "first_identity",
        lambda: None if first[0] is None else tuple(first),
        raising=False,
    )
    return first


@pytest.fixture
def client(identity, store, monkeypatch):
    monkeypatch.setattr(
        scim_groups,
        "authenticate_token",
        AsyncMock(side_effect=functools.partial(verify_token, secret=JWT_SECRET)),
    )
    api = FastAPI()
    scim_groups.attach(api)
    scim_routes.attach(api)
    return TestClient(api)


@pytest.mark.parametrize("presented", [OWN, NEXT, "wrong"])
def test_every_configured_candidate_is_compared(identity, monkeypatch, presented):
    monkeypatch.setattr(settings, "scim_token", f"{OWN},{NEXT}")
    compare = hmac.compare_digest
    seen = []

    def compared(left, right):
        seen.append((left, right))
        return compare(left, right)

    monkeypatch.setattr(scim.hmac, "compare_digest", compared)
    assert scim.authorized(presented) is (presented in (OWN, NEXT))
    assert seen == [(presented.encode(), OWN.encode()), (presented.encode(), NEXT.encode())]


@pytest.mark.parametrize("raw", ["", OWN, FOREIGN, "lsc_malformed", f"{OWN},{NEXT},{FOREIGN}"])
def test_no_cell_keeps_existing_token_comparison_without_reading_identity(monkeypatch, raw):
    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(settings, "scim_token", raw)

    def forbidden():
        pytest.fail("single-deployment authorization must not read cell identity")

    monkeypatch.setattr(cell, "first_identity", forbidden, raising=False)
    assert scim.authorized(FOREIGN) is (FOREIGN in scim.tokens(raw))


def test_zero_tokens_do_not_consult_cell_identity(identity, monkeypatch):
    monkeypatch.setattr(settings, "scim_token", "")

    def forbidden():
        pytest.fail("SCIM off must not consult cell identity")

    monkeypatch.setattr(cell, "first_identity", forbidden)
    assert scim.authorized("org-admin") is False


def test_unknown_identity_is_availability_not_an_ordinary_nonmatch(identity):
    identity[0] = None
    with pytest.raises(AuthUnavailable):
        scim.authorized(OWN)


@pytest.mark.parametrize("path", ["/scim/v2/ServiceProviderConfig", "/scim/v2/Groups"])
@pytest.mark.parametrize(
    "configured,first,presented,status",
    [
        (OWN, (True, "first"), OWN, 200),
        (OWN, (False, "first"), OWN, 200),
        (LEGACY, (True, "first"), LEGACY, 200),
        (LEGACY, (False, "first"), LEGACY, 503),
        (OWN, (None, "first"), OWN, 503),
        (OWN, (True, "another"), OWN, 503),
        (FOREIGN, (True, "first"), FOREIGN, 401),
        (f"{OWN},{NEXT},{OWN}", (True, "first"), OWN, 503),
        (OWN, (True, "first"), FOREIGN, 401),
        (OWN, (True, "first"), "lsc_first_malformed", 401),
    ],
)
def test_real_route_families_share_cell_policy(
    client, identity, monkeypatch, path, configured, first, presented, status
):
    identity[:] = first
    monkeypatch.setattr(settings, "scim_token", configured)
    response = client.get(path, headers={"Authorization": f"Bearer {presented}"})
    assert response.status_code == status
    if status == 503:
        assert response.headers["Retry-After"] == "5"
    if status != 200:
        assert presented not in response.text


def test_groups_retains_its_real_org_admin_bearer_alternative(client):
    token = mint_token("operator", "acme", ["*"], ["org_admin"], secret=JWT_SECRET)
    response = client.get("/scim/v2/Groups", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200 and response.json()["totalResults"] == 0


@pytest.mark.parametrize("token", [FOREIGN, "lsc_first_malformed"])
def test_reserved_cell_token_cannot_fall_through_as_a_groups_admin(client, monkeypatch, token):
    def forbidden(_token):
        pytest.fail("a reserved SCIM credential must not use the org-admin alternative")

    monkeypatch.setattr(scim_groups, "authenticate_token", AsyncMock(side_effect=forbidden))
    response = client.get("/scim/v2/Groups", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_groups_availability_failure_cannot_use_an_admin_fallback(client, identity, monkeypatch):
    identity[0] = None

    def forbidden(_token):
        pytest.fail("an unread SCIM cell must not fall through unchecked")

    monkeypatch.setattr(scim_groups, "authenticate_token", AsyncMock(side_effect=forbidden))
    response = client.get("/scim/v2/Groups", headers={"Authorization": "Bearer org-admin"})
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
