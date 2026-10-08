"""Every answer names one wire schema version: the `X-Schema-Version` header
of an op response and `ops.capabilities.schema_version` agree."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.operations.capabilities import SCHEMA_VERSION, capability_snapshot
from server.settings import settings


def test_the_header_and_the_catalogue_carry_one_version(monkeypatch) -> None:
    from server.transport.http import build_router

    monkeypatch.setattr(settings, "auth_required", False)
    app = FastAPI()
    app.include_router(build_router())
    r = TestClient(app).post("/v1/ops.capabilities", json={})
    assert r.status_code == 200, r.text
    assert r.headers["X-Schema-Version"] == SCHEMA_VERSION == r.json()["schema_version"]
    assert capability_snapshot()["schema_version"] == SCHEMA_VERSION
