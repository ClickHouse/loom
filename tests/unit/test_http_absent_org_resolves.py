"""An HTTP body whose `org` or `namespace` is absent to the gates (blank or
the "-" sentinel) runs under the target the gate authorized: the token's
org, never the literal "-". Otherwise a token of one org writes rows under
org "-", and a token of any other org reads them back the same way."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
from server.memory.api.models_crud import MemoryGetOutput
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings


@pytest.fixture()
def seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    from server.transport import http as http_mod

    calls: list[tuple[str, str]] = []

    async def _handler(inp: Any) -> MemoryGetOutput:
        calls.append((inp.org, inp.namespace))
        return MemoryGetOutput(found=False)

    ops = [
        dataclasses.replace(op, handler=_handler) if op.name == "memory.get" else op
        for op in ALL_OPERATIONS
    ]
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    return calls


def _client() -> TestClient:
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    return TestClient(app)


@pytest.mark.parametrize("org", ["-", "", "  "])
def test_an_absent_org_runs_under_the_tokens_org(seen: list[tuple[str, str]], org: str) -> None:
    tok = mint_token(
        subject="probe", org="globex", namespaces=["ns1"], roles=["viewer"], ttl_seconds=300
    )
    r = _client().post(
        "/v1/memory.get",
        json={"org": org, "namespace": "ns1", "agent": "a", "memory_key": "k"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    assert seen == [("globex", "ns1")]
