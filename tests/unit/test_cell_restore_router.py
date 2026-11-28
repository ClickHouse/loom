"""Restore authenticates and validates a bounded request before reading the directory."""

from __future__ import annotations

import hashlib

import pytest
from starlette.testclient import TestClient

from server.router import app, directory
from server.router.limits import MAX_BODY_BYTES
from server.settings import settings

TOKEN = "lco_" + "7" * 128


@pytest.fixture
def endpoint(monkeypatch):
    calls = []

    async def read(*args):
        calls.append(args)
        return {"descriptor": {}, "sha256": "a" * 64, "orgs": [], "done": True, "next": None}

    monkeypatch.setattr(directory, "restore_read", read, raising=False)
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(TOKEN.encode()).hexdigest()
    )
    return TestClient(app.build_app(app.Router())), calls


def post(client, **over):
    return client.post(
        "/cells/v1/operator/cells/first/restore-read",
        json={
            "deployment_id": "dep",
            "after": None,
            "expected_sha256": None,
            **over,
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_operator_auth_before_any_directory_read(endpoint):
    client, calls = endpoint
    assert client.post("/cells/v1/operator/cells/first/restore-read", json={}).status_code == 401
    assert calls == []


@pytest.mark.parametrize(
    "over",
    [
        {"after": "-"},
        {"after": 1},
        {"deployment_id": ""},
        {"expected_sha256": "bad"},
        {"extra": True},
    ],
)
def test_malformed_request_before_directory(endpoint, over):
    client, calls = endpoint
    assert post(client, **over).status_code == 400
    assert calls == []


def test_body_limit_before_directory(endpoint):
    client, calls = endpoint
    assert (
        client.post(
            "/cells/v1/operator/cells/first/restore-read",
            content=b"x" * (MAX_BODY_BYTES + 1),
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).status_code
        == 413
    )
    assert calls == []


def test_operator_page_is_not_a_placement(endpoint):
    client, calls = endpoint
    assert post(client).status_code == 200
    assert calls == [("first", "dep", None, None)]
